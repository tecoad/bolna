import asyncio
import traceback
import numpy as np
import torch
import websockets
import os
import json
import aiohttp
import time
from dotenv import load_dotenv
from .base_transcriber import BaseTranscriber
from bolna.helpers.logger_config import configure_logger
from bolna.helpers.utils import create_ws_data_packet, int2float
from bolna.helpers.vad import VAD

torch.set_num_threads(1)

logger = configure_logger(__name__)
load_dotenv()


class DeepgramTranscriber(BaseTranscriber):
    def __init__(self, provider, input_queue=None, model='deepgram', stream=True, language="en", endpointing="400",
                 sampling_rate="16000", encoding="linear16", output_queue= None, keywords = None, **kwargs):
        super().__init__(input_queue)
        self.endpointing = endpointing
        self.language = language
        self.stream = stream
        self.provider = provider
        self.heartbeat_task = None
        self.sender_task = None
        self.model = 'deepgram'
        self.sampling_rate = sampling_rate
        self.encoding = encoding
        self.api_key = kwargs.get("transcriber_key", os.getenv('DEEPGRAM_AUTH_TOKEN'))
        self.transcriber_output_queue = output_queue
        self.transcription_task = None
        self.on_device_vad = kwargs.get("on_device_vad", False) if self.stream else False
        self.keywords = keywords
        logger.info(f"self.stream: {self.stream}")
        if self.on_device_vad:
            self.vad_model = VAD()
            self.audio = []
            # logger.info("on_device_vad is TRue")
            # self.vad_model, self.vad_utils = torch.hub.load(repo_or_dir='snakers4/silero-vad', model='silero_vad', force_reload=False)
        self.voice_threshold = 0.5
        self.interruption_signalled = False
        self.sampling_rate = 16000
        if not self.stream:
            self.api_url = f"https://api.deepgram.com/v1/listen?model=nova-2&filler_words=true&language={self.language}"
            self.session = aiohttp.ClientSession()
            if self.keywords is not None:
                keyword_string = "&keywords=" + "&keywords=".join(self.keywords.split(","))
                self.api_url = f"{self.api_url}{keyword_string}"
        self.audio_submitted = False
        self.audio_submission_time = None
        self.num_frames = 0
        self.connection_start_time = None
        self.process_interim_results = "true"
        #Work on this soon
        self.last_utterance_time_stamp = time.time()
        self.utterance_end_task= None
    
    def __get_speaker_transcript(self, data):
        transcript_words = []
        if 'channel' in data and 'alternatives' in data['channel']:
            for alternative in data['channel']['alternatives']:
                if 'words' in alternative:
                    for word_info in alternative['words']:
                        if word_info['speaker'] == 0:
                            transcript_words.append(word_info['word'])

        return ' '.join(transcript_words)

    def get_deepgram_ws_url(self):
        websocket_url = (f"wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=16000&channels=1"
                         f"&filler_words=true&interim_results={self.process_interim_results}&diarize=true&utterance_end_ms=1000")
        self.audio_frame_duration = 0.5 #We're sending 8k samples with a sample rate of 16k

        if self.provider in ('twilio', 'exotel'):
            self.sampling_rate = 8000
            self.audio_frame_duration = 0.2  # With telephony we are sending 100ms at a time

            if self.provider == 'twilio':
                self.encoding = 'mulaw'

            websocket_url = (f"wss://api.deepgram.com/v1/listen?model=nova-2&encoding={self.encoding}&sample_rate={self.sampling_rate}&channels"
                             f"=1&filler_words=true&interim_results={self.process_interim_results}&diarize=true&utterance_end_ms=1000")

        if self.provider == "playground":
            websocket_url = (f"wss://api.deepgram.com/v1/listen?model=nova-2&encoding=opus&sample_rate=8000&channels"
                             f"=1&filler_words=true&interim_results={self.process_interim_results}&diarize=true&utterance_end_ms=1000")
            self.sampling_rate = 8000
            self.audio_frame_duration = 0.0 #There's no streaming from the playground 

        if "en" not in self.language:
            websocket_url += '&language={}'.format(self.language)
        
        if self.keywords is not None:
            keyword_string = "&keywords=" + "&keywords=".join(self.keywords.split(","))
            websocket_url = f"{websocket_url}{keyword_string}"
        logger.info(f"Deepgram websocket url: {websocket_url}")
        return websocket_url

    async def send_heartbeat(self, ws):
        try:
            while True:
                data = {'type': 'KeepAlive'}
                await ws.send(json.dumps(data))
                await asyncio.sleep(5)  # Send a heartbeat message every 5 seconds
        except Exception as e:
            logger.error('Error while sending: ' + str(e))
            raise Exception("Something went wrong while sending heartbeats to {}".format(self.model))

    async def toggle_connection(self):
        self.connection_on = False
        if self.heartbeat_task is not None:
            self.heartbeat_task.cancel()
        self.sender_task.cancel()

    async def _get_http_transcription(self, audio_data):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

        headers = {
            'Authorization': 'Token {}'.format(self.api_key),
            'Content-Type': 'audio/webm'  # Currently we are assuming this is via browser
        }

        self.current_request_id = self.generate_request_id()
        self.meta_info['request_id'] = self.current_request_id
        start_time = time.time()
        async with self.session as session:
            async with session.post(self.api_url, data=audio_data, headers=headers) as response:
                response_data = await response.json()
                logger.info(f"response_data {response_data} total time {time.time() - start_time}")
                transcript = response_data["results"]["channels"][0]["alternatives"][0]["transcript"]
                logger.info(f"transcript {transcript} total time {time.time() - start_time}")
                self.meta_info['transcriber_duration'] = response_data["metadata"]["duration"]
                return create_ws_data_packet(transcript, self.meta_info)

    async def _check_and_process_end_of_stream(self, ws_data_packet, ws):
        if 'eos' in ws_data_packet['meta_info'] and ws_data_packet['meta_info']['eos'] is True:
            logger.info("First closing transcription websocket")
            await self._close(ws, data={"type": "CloseStream"})
            logger.info("Closed transcription websocket and now closing transcription task")
            return True  # Indicates end of processing

        return False

    def get_meta_info(self):
        return self.meta_info
    
    async def sender(self, ws=None):
        try:
            while True:
                ws_data_packet = await self.input_queue.get()
                #If audio submitted was false, that means that we're starting the stream now. That's our stream start
                if self.audio_submitted == False:
                    self.audio_submitted = True
                    self.audio_submission_time = time.time()
                end_of_stream = await self._check_and_process_end_of_stream(ws_data_packet, ws)
                if end_of_stream:
                    break
                self.meta_info = ws_data_packet.get('meta_info')
                start_time = time.time()
                transcription = await self._get_http_transcription(ws_data_packet.get('data'))
                transcription['meta_info']["include_latency"] = True
                transcription['meta_info']["transcriber_latency"] = time.time() - start_time
                transcription['meta_info']['audio_duration'] = transcription['meta_info']['transcriber_duration']
                transcription['meta_info']['last_vocal_frame_timestamp'] = start_time
                yield transcription

            if self.transcription_task is not None:
                self.transcription_task.cancel()
        except asyncio.CancelledError:
            logger.info("Cancelled sender task")
            return
        except Exception as e:
            logger.error('Error while sending: ' + str(e))
            raise Exception("Something went wrong")

    async def __check_for_vad(self, data):
        if data is None:
            return
        self.audio.append(data)
        audio_bytes = b''.join(self.audio)
        audio_int16 = np.frombuffer(audio_bytes, np.int16)
        frame_np = int2float(audio_int16)
        
        speech_prob = self.vad_model(torch.from_numpy(frame_np.copy()), self.sampling_rate).item()
        logger.info(f"Speech probability {speech_prob}")
        if float(speech_prob) >= float(self.voice_threshold):
            logger.info(f"It's definitely human voice and hence interrupting {self.meta_info}")
            self.interruption_signalled = True
            await self.push_to_transcriber_queue(create_ws_data_packet("INTERRUPTION", self.meta_info))
            self.audio = []

        #logger.info(f"Time to run VAD {time.time() - start_time}")
    async def sender_stream(self, ws=None):
        try:
            while True:
                ws_data_packet = await self.input_queue.get() 
                #Initialise new request
                if self.audio_submitted == False:
                    self.meta_info = ws_data_packet.get('meta_info')
                    self.audio_submitted = True
                    self.audio_submission_time = time.time()
                    self.current_request_id = self.generate_request_id()
                    self.meta_info['request_id'] = self.current_request_id

                audio_bytes = ws_data_packet['data']
                if not self.interruption_signalled and self.on_device_vad:
                    await self.__check_for_vad(audio_bytes)
                end_of_stream = await self._check_and_process_end_of_stream(ws_data_packet, ws)
                if end_of_stream:
                    break
                self.num_frames +=1
                await ws.send(ws_data_packet.get('data'))

        except Exception as e:
            logger.error('Error while sending: ' + str(e))
            raise Exception("Something went wrong")

    async def receiver(self, ws):
        curr_message = ""
        finalized_transcript= ""
        async for msg in ws:
            try:
                msg = json.loads(msg)

                #If connection start time is None, connection start time is the duratons of frame submitted till now minus current time
                if self.connection_start_time is None:
                    self.connection_start_time = (time.time() - (self.num_frames * self.audio_frame_duration))
                    logger.info(f"Connecton start time {self.connection_start_time} {self.num_frames} and {self.audio_frame_duration}")

                logger.info(f"###### ######### ############# Message from the transcriber {msg}")
                if msg['type'] == "Metadata":
                    logger.info(f"Got a summary object {msg}")
                    self.meta_info["transcriber_duration"] = msg["duration"]
                    yield create_ws_data_packet("transcriber_connection_closed", self.meta_info)
                    return
                
                #TODO LATENCY STUFF
                if msg["type"] == "UtteranceEnd":
                    logger.info("Transcriber Latency: {} for request id {}".format(time.time() - self.audio_submission_time, self.current_request_id))
                    logger.info(f"Current message during UtteranceEnd {curr_message}")
                    self.meta_info["start_time"] = self.audio_submission_time
                    self.meta_info["end_time"] = time.time() - 100
                    self.meta_info['speech_final'] = True
                    self.audio_submitted = False
                    self.meta_info["include_latency"] = True
                    self.meta_info["audio_duration"] = 10
                    last_spoken_audio_frame = self.connection_start_time + 10
                    self.meta_info["audio_start_time"] = self.audio_submission_time 
                    transcription_completion_time = time.time()
                    self.meta_info["transcription_completion_time"] = transcription_completion_time
                    # abs because sometimes it's negative. Got to debug that further  
                    self.meta_info["last_vocal_frame_timestamp"] = last_spoken_audio_frame
                    if curr_message == "":
                        continue
                    logger.info(f"Signalling the Task manager to start speaking")
                    yield create_ws_data_packet(finalized_transcript, self.meta_info)
                    curr_message = ""
                    finalized_transcript = ""
                    continue
                
                # if msg["type"] == "SpeechStarted":
                #     if not self.on_device_vad:
                #         logger.info("Not on device vad and hence inetrrupting")
                #         yield create_ws_data_packet("TRANSCRIBER_BEGIN", self.meta_info)
                #     continue

                transcript = msg['channel']['alternatives'][0]['transcript']

                if transcript and len(transcript.strip()) == 0 or transcript == "":
                    continue
                
                #TODO Remove the need for on_device_vad
                # If interim message is not true and curr message is null, send a begin signal
                if curr_message == "" and msg["is_final"] == False:
                    if not self.on_device_vad:
                        logger.info("Not on device vad and hence inetrrupting")
                        self.meta_info["should_interrupt"] = True
                    yield create_ws_data_packet("TRANSCRIBER_BEGIN", self.meta_info)

                    await asyncio.sleep(0.1) #Enable taskmanager to interrupt

                #Do not send back interim results, just send back interim message
                if self.process_interim_results and msg["is_final"] == True:    
                    logger.info(f"Is final interim Transcriber message {msg}")
                    #curr_message = self.__get_speaker_transcript(msg)
                    finalized_transcript += " " + transcript #Just get the whole transcript as there's mismatch at times
                    self.meta_info["is_final"] = True
                    if transcript.strip() != curr_message.strip():
                        yield create_ws_data_packet(curr_message, self.meta_info)
                else:
                    #If we're not processing interim results
                    # Yield current transcript
                    #curr_message = self.__get_speaker_transcript(msg)
                    # Just yield the current transcript as we do not want to wait for is_final. Is_final is just to make 
                    curr_message = finalized_transcript + " " + transcript
                    logger.info(f"Yielding interim-message current_message = {curr_message}")
                    self.meta_info["include_latency"] = False
                    self.meta_info["utterance_end"] = self.__calculate_utterance_end(msg)
                    self.meta_info["time_received"] = time.time()
                    self.meta_info["transcriber_latency"] =  self.meta_info["time_received"] - self.meta_info["utterance_end"] 
                    yield create_ws_data_packet(curr_message, self.meta_info)
                    # #If the current message is empty no need to send anything to the task manager
                    # if curr_message == "":
                    #     continue
                    #yield create_ws_data_packet(curr_message, self.meta_info)
                    #curr_message = ""
            except Exception as e:
                traceback.print_exc()
                logger.error(f"Error while getting transcriptions {e}")
                self.interruption_signalled = False
                yield create_ws_data_packet("TRANSCRIBER_END", self.meta_info)

    async def push_to_transcriber_queue(self, data_packet):
        await self.transcriber_output_queue.put(data_packet)

    def deepgram_connect(self):
        websocket_url = self.get_deepgram_ws_url()
        extra_headers = {
            'Authorization': 'Token {}'.format(os.getenv('DEEPGRAM_AUTH_TOKEN'))
        }
        deepgram_ws = websockets.connect(websocket_url, extra_headers=extra_headers)
        return deepgram_ws

    async def run(self):
        self.transcription_task = asyncio.create_task(self.transcribe())

    def __calculate_utterance_end(self,data):
        utterance_end = ''
        if 'channel' in data and 'alternatives' in data['channel']:
            for alternative in data['channel']['alternatives']:
                if 'words' in alternative:
                    final_word =  alternative['words'][-1]
                    utterance_end = self.connection_start_time + final_word['end'] 
                    logger.info(f"Final word ended at {utterance_end}")
        return utterance_end

    async def transcribe(self):
        try:
            async with self.deepgram_connect() as deepgram_ws:
                if self.stream:
                    self.sender_task = asyncio.create_task(self.sender_stream(deepgram_ws))
                    self.heartbeat_task = asyncio.create_task(self.send_heartbeat(deepgram_ws))
                    async for message in self.receiver(deepgram_ws):
                        if self.connection_on:
                            await self.push_to_transcriber_queue(message)
                        else:
                            logger.info("closing the deepgram connection")
                            await self._close(deepgram_ws, data={"type": "CloseStream"})
                else:
                    async for message in self.sender():
                        await self.push_to_transcriber_queue(message)
            
            await self.push_to_transcriber_queue(create_ws_data_packet("transcriber_connection_closed", self.meta_info))
        except Exception as e:
            logger.error(f"Error in transcribe: {e}")