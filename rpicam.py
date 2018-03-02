#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gi
gi.require_version('Gst','1.0')
from gi.repository import Gst

import picamera
import numpy as np
import sys
import os
import psutil
import threading

FORMAT_H264 = 0
FORMAT_MJPEG = 1

RTP_PORT = 5000

# Возвращает температуру процессора
def getCPUtemperature():
    res = os.popen('vcgencmd measure_temp').readline()
    return float(res.replace('temp=','').replace('\'C\n',''))

# проверка доступности камеры, возвращает True, если камера доступна в системе
def checkCamera():
    res = os.popen('vcgencmd get_camera').readline().replace('\n','') #читаем результат, удаляем \n
    dct = {}
    for param in res.split(' '): #разбираем параметры
        tmp = param.split('=')
        dct.update({tmp[0]: tmp[1]}) #помещаем в словарь
    return (dct['supported'] and dct['detected'])

def getIP():
    #cmd = 'hostname -I | cut -d\' \' -f1'
    #ip = subprocess.check_output(cmd, shell = True) #получаем IP
    res = os.popen('hostname -I | cut -d\' \' -f1').readline().replace('\n','') #получаем IP, удаляем \n
    return res

class AppSrcStreamer(object):
    def __init__(self, video = FORMAT_H264, resolution = (640, 480), framerate = 30, host = ('localhost', RTP_PORT), onFrameCallback = None):        
        self.size = 0
        self._host = host
        self._width = resolution[0]
        self._height = resolution[1]
        self._needFrame = threading.Event() #флаг, необходимо сформировать OpenCV кадр
        self.playing = False
        self.paused = False
        self._onFrameCallback = None
        if (not onFrameCallback is None) and callable(onFrameCallback):
            self._onFrameCallback = onFrameCallback #обработчик события OpenCV кадр готов
        #инициализация Gstreamer
        Gst.init(None)
        self.make_pipeline(video, self._width, self._height, framerate, host)
        
    def make_pipeline(self, video, width, height, framerate, host):     
        # Создание GStreamer pipeline
        self.pipeline = Gst.Pipeline()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.onMessage)

        rtpbin = Gst.ElementFactory.make('rtpbin')
        rtpbin.set_property('drop-on-latency', True) #отбрасывать устаревшие кадры
                
        #настраиваем appsrc
        self.appsrc = Gst.ElementFactory.make('appsrc')
        self.appsrc.set_property("is-live", True)
        videoStr = 'video/x-h264'
        if video:
            videoStr = 'image/jpeg'
        capstring = videoStr + ',width=' + str(width) \
            + ',height=' + str(height) + ',framerate=' \
            + str(framerate)+'/1'   
        srccaps = Gst.Caps.from_string(capstring)
        self.appsrc.set_property("caps", srccaps)
        #print('RPi camera GST caps: %s' % capstring)

        if video == FORMAT_H264:
            parse = Gst.ElementFactory.make('h264parse')
        else:
            parse = Gst.ElementFactory.make('jpegparse')
        
        if video == FORMAT_H264:
            pay = Gst.ElementFactory.make('rtph264pay')
            #rtph264pay.set_property("config-interval", 10)
            pay.set_property("pt", 96)
        else:
            pay = Gst.ElementFactory.make('rtpjpegpay')
            pay.set_property("pt", 26)

        #For RTP Video
        udpsink_rtpout = Gst.ElementFactory.make('udpsink', 'udpsink_rtpout')
        udpsink_rtpout.set_property('host', host[0])
        udpsink_rtpout.set_property('port', host[1])

        udpsink_rtcpout = Gst.ElementFactory.make('udpsink', 'udpsink_rtcpout')
        udpsink_rtcpout.set_property('host', host[0])
        udpsink_rtcpout.set_property('port', host[1] + 1)
        udpsink_rtcpout.set_property('sync', False)
        udpsink_rtcpout.set_property('async', False)

        udpsrc_rtcpin = Gst.ElementFactory.make('udpsrc', 'udpsrc_rtcpin')
        udpsrc_rtcpin.set_property('port', host[1] + 5)

        if not self._onFrameCallback is None:
            tee = Gst.ElementFactory.make('tee')
            rtpQueue = Gst.ElementFactory.make('queue', 'rtp_queue')
            frameQueue = Gst.ElementFactory.make('queue', 'frame_queue')
        
            if video == FORMAT_H264: 
                #decoder = Gst.ElementFactory.make('avdec_h264') #хреново работает загрузка ЦП 120%
                decoder = Gst.ElementFactory.make('omxh264dec') #отлично работает загрузка ЦП 200%
                #decoder = Gst.ElementFactory.make('avdec_h264_mmal') #не заработал
            else:
                #decoder = Gst.ElementFactory.make('avdec_mjpeg') #
                decoder = Gst.ElementFactory.make('omxmjpegdec') #
                #decoder = Gst.ElementFactory.make('jpegdec') #
            
            
            videoconvert = Gst.ElementFactory.make('videoconvert')

            def newSample(sink, data):     # callback функция, исполняющаяся при каждом приходящем кадре
                if self._needFrame.is_set(): #если выставлен флаг нужен кадр
                    sample = sink.emit("pull-sample")
                    sampleBuff = sample.get_buffer()

                    #создаем массив cvFrame в формате opencv
                    cvFrame = np.ndarray(
                        (self._height, self._width, 3),
                        buffer = sampleBuff.extract_dup(0, sampleBuff.get_size()), dtype = np.uint8)
            
                    self._onFrameCallback(cvFrame) #вызываем обработчик в качестве параметра передаем cv2 кадр
                    
                    self._needFrame.clear() #сбрасываем флаг
                return Gst.FlowReturn.OK
        
            ### создаем свой sink для перевода из GST в CV
            appsink = Gst.ElementFactory.make('appsink')

            cvcaps = Gst.caps_from_string("video/x-raw, format=(string){BGR, GRAY8}") # формат приема sink'a
            appsink.set_property("caps", cvcaps)
            appsink.set_property("sync", False)
            appsink.set_property("async", False)
            appsink.set_property("drop", True)
            appsink.set_property("max-buffers", 1)
            appsink.set_property("emit-signals", True)
            appsink.connect("new-sample", newSample, appsink)

        # добавляем все элементы в pipeline
        elemList = [self.appsrc, rtpbin, parse, pay, udpsink_rtpout,
                    udpsink_rtcpout, udpsrc_rtcpin]
        if not self._onFrameCallback is None:
            elemList.extend([tee, rtpQueue, frameQueue, decoder, videoconvert, appsink])
            
        for elem in elemList:
            self.pipeline.add(elem)

        #соединяем элементы
        ret = self.appsrc.link(parse)

        #соединяем элементы rtpbin
        ret = ret and pay.link_pads('src', rtpbin, 'send_rtp_sink_0')
        ret = ret and rtpbin.link_pads('send_rtp_src_0', udpsink_rtpout, 'sink')
        ret = ret and rtpbin.link_pads('send_rtcp_src_0', udpsink_rtcpout, 'sink')
        ret = ret and udpsrc_rtcpin.link_pads('src', rtpbin, 'recv_rtcp_sink_0')

        if self._onFrameCallback is None: #трансляция без OpenCV, т.е. создаем одну ветку
            ret = ret and parse.link(pay)
            
        else: #трансляция с передачей кадров в onFrameCallback, создаем две ветки
            ret = ret and parse.link(tee)
            
            #1-я ветка RTP
            ret = ret and rtpQueue.link(pay)

            #2-я ветка onFrame
            ret = ret and frameQueue.link(decoder)
            ret = ret and decoder.link(videoconvert)
            ret = ret and videoconvert.link(appsink)

            # подключаем tee к rtpQueue
            teeSrcPadTemplate = tee.get_pad_template('src_%u')
        
            rtpTeePad = tee.request_pad(teeSrcPadTemplate, None, None)
            rtpQueuePad = rtpQueue.get_static_pad('sink')
            ret = ret and (rtpTeePad.link(rtpQueuePad) == Gst.PadLinkReturn.OK)

            # подключаем tee к frameQueue
            frameTeePad = tee.request_pad(teeSrcPadTemplate, None, None)
            frameQueuePad = frameQueue.get_static_pad('sink')        
            ret = ret and (frameTeePad.link(frameQueuePad) == Gst.PadLinkReturn.OK)

        if not ret:
            print('ERROR: Elements could not be linked')
            sys.exit(1)
            
    def onMessage(self, bus, message):
        #print('Message: %s' % str(message.type))
        t = message.type
        if t == Gst.MessageType.EOS:
            print('Received EOS-Signal')
            self.stop_pipeline()
        elif t == Gst.MessageType.ERROR:
            print('Received Error-Signal')
            error, debug = message.parse_error()
            print('Error-Details: #%u: %s' % (error.code, debug))
            self.null_pipeline()
        #else:
        #    print("Message: %s" % str(t))

    def play_pipeline(self):
        self.pipeline.set_state(Gst.State.PLAYING)
        print('GST pipeline PLAYING')
        print('Streaming RTP on %s:%d' % (self._host[0], self._host[1]))

    def stop_pipeline(self):
        self.pipeline.set_state(Gst.State.PAUSED)
        print('GST pipeline PAUSED')
        self.pipeline.set_state(Gst.State.READY)
        print('GST pipeline READY')

    def null_pipeline(self):
        print('GST pipeline NULL')
        self.pipeline.set_state(Gst.State.NULL)

    def write(self, s):
        gstBuff = Gst.Buffer.new_wrapped(s)
        if not gstBuff is None:
            self.appsrc.emit("push-buffer", gstBuff)

    def flush(self):
        self.stop_pipeline()

    def frameRequest(self): #выставляем флаг запрос кадра, возвращает True, если флаг выставлен
        if not self._needFrame.is_set():
            self._needFrame.set()
        return self._needFrame.is_set()

class RPiCamStreamer(object):
    def __init__(self, video = FORMAT_H264, resolution = (640, 480), framerate = 30, host = ('localhost', RTP_PORT), onFrameCallback = None):
        self._videoFormat = 'h264'
        self._quality = 20
        self._bitrate = 1000000
        if video:
            self._videoFormat = 'mjpeg'
            self._quality = 60
            self._bitrate = 8000000
        self.camera = picamera.PiCamera()
        self.camera.resolution = resolution
        self.camera.framerate = framerate
        self._stream = AppSrcStreamer(video, resolution,
            framerate, host, onFrameCallback)

    def start(self):
        print('Start RPi camera recording: %s:%dx%d, framerate=%d, bitrate=%d, quality=%d'
              % (self._videoFormat, self.camera.resolution[0], self.camera.resolution[1],
                 self.camera.framerate, self._bitrate, self._quality))
        self._stream.play_pipeline() #запускаем трансляцию
        self.camera.start_recording(self._stream, self._videoFormat, bitrate=self._bitrate, quality=self._quality)

    def stop(self):
        print('Stop RPi camera recording')
        self.camera.stop_recording()

    def close(self):
        self._stream.null_pipeline() #закрываем трансляцию
        self.camera.close()

    def frameRequest(self): #выставляем флаг запрос кадра, возвращает True, если флаг выставлен
        return self._stream.frameRequest()

    def setFlip(self, hflip, vflip):
        self.camera.hflip = hflip
        self.camera.vflip = vflip
        
    def setRotation(self, rotation):
        self.camera.rotation = rotation
