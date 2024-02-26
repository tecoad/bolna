import json
import asyncio
import re
import numpy as np
import copy
import hashlib
import os
import traceback
from botocore.exceptions import BotoCoreError, ClientError
from aiobotocore.session import AioSession
from contextlib import AsyncExitStack
from dotenv import load_dotenv
from pydantic import BaseModel, create_model
import wave
import io
import numpy as np
from scipy.io import wavfile

import torch
import torchaudio
from .logger_config import configure_logger
from bolna.constants import PREPROCESS_DIR
from pydub import AudioSegment

logger = configure_logger(__name__)
load_dotenv()
BUCKET_NAME = os.getenv('BUCKET_NAME')


class DictWithMissing(dict):
    def __missing__(self, key):
        return ''


def load_file(file_path, is_json=False):
    data = None
    with open(file_path, "r") as f:
        if is_json:
            data = json.load(f)
        else:
            data = f.read()

    return data


def write_json_file(file_path, data):
    with open(file_path, 'w') as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def create_ws_data_packet(data, meta_info=None, is_md5_hash=False, llm_generated=False):
    metadata = copy.deepcopy(meta_info)
    if meta_info is not None: #It'll be none in case we connect through dashboard playground
        metadata["is_md5_hash"] = is_md5_hash
        metadata["llm_generated"] = llm_generated
    return {
        'data': data,
        'meta_info': metadata
    }


def int2float(sound):
    abs_max = np.abs(sound).max()
    sound = sound.astype('float32')
    if abs_max > 0:
        sound *= 1 / 32768
    sound = sound.squeeze()  # depends on the use case
    return sound


def float2int(sound):
    sound = np.int16(sound * 32767)
    return sound


def mu_law_encode(audio, quantization_channels=256):
    mu = quantization_channels - 1
    safe_audio_abs = np.minimum(np.abs(audio), 1.0)
    magnitude = np.log1p(mu * safe_audio_abs) / np.log1p(mu)
    signal = np.sign(audio) * magnitude
    return ((signal + 1) / 2 * mu + 0.5).astype(np.int32)


def float32_to_int16(float_audio):
    float_audio = np.clip(float_audio, -1.0, 1.0)
    int16_audio = (float_audio * 32767).astype(np.int16)
    return int16_audio

def wav_bytes_to_pcm(wav_bytes):
    wav_buffer = io.BytesIO(wav_bytes)
    rate, data = wavfile.read(wav_buffer)
    if data.dtype == np.int16:
        return data.tobytes()
    if data.dtype == np.float32:
        data = float32_to_int16(data)
        return data.tobytes()


# def wav_bytes_to_pcm(wav_bytes):
#     wav_buffer = io.BytesIO(wav_bytes)
#     with wave.open(wav_buffer, 'rb') as wav_file:
#         pcm_data = wav_file.readframes(wav_file.getnframes())
#     return pcm_data

# def wav_bytes_to_pcm(wav_bytes):
#     wav_buffer = io.BytesIO(wav_bytes)
#     audio = AudioSegment.from_file(wav_buffer, format="wav")
#     pcm_data = audio.raw_data
#     return pcm_data



def raw_to_mulaw(raw_bytes):
    # Convert bytes to numpy array of int16 values
    samples = np.frombuffer(raw_bytes, dtype=np.int16)
    samples = samples.astype(np.float32) / (2 ** 15)
    mulaw_encoded = mu_law_encode(samples)
    return mulaw_encoded


async def get_s3_file(bucket_name, file_key):
    session = AioSession()

    async with AsyncExitStack() as exit_stack:
        s3_client = await exit_stack.enter_async_context(session.create_client('s3'))
        try:
            response = await s3_client.get_object(Bucket=bucket_name, Key=file_key)
        except (BotoCoreError, ClientError) as error:
            logger.error(error)
        else:
            file_content = await response['Body'].read()
            return file_content


async def store_file(bucket_name = None, file_key = None, file_data = None, content_type= "json", local = False, preprocess_dir = None):
    
    if not local:
        session = AioSession()

        async with AsyncExitStack() as exit_stack:
            s3_client = await exit_stack.enter_async_context(session.create_client('s3'))
            data = None
            if content_type == "json":
                data = json.dumps(file_data)
            elif content_type in ["mp3", "wav", "pcm", "csv"]:
                data = file_data
            try:
                await s3_client.put_object(Bucket=bucket_name, Key=file_key, Body=data)
            except (BotoCoreError, ClientError) as error:
                logger.error(error)
            except Exception as e:
                logger.error('Exception occurred while s3 put object: {}'.format(e))
    if local:
        dir = PREPROCESS_DIR if preprocess_dir == None else preprocess_dir
        directory_path = os.path.join(dir, os.path.dirname(file_key))
        logger.info(file_data)
        os.makedirs(directory_path, exist_ok=True)
        if content_type == "json":
            logger.info(f"Writing to {dir}/{file_key} ")
            with open(f"{dir}/{file_key}", 'w') as f:
                data = json.dumps(file_data)
                f.write(data)
        elif content_type in ["mp3", "wav", "pcm", "csv"]:
            with open(f"{dir}/{file_key}", 'w') as f:
                data = file_data
                f.write(data)

async def get_raw_audio_bytes_from_base64(agent_name, b64_string, audio_format='mp3', assistant_id=None, local = False):
    # we are already storing pcm formatted audio in the filler config. No need to encode/decode them further
    audio_data = None
    if local:
        file_name = f"{PREPROCESS_DIR}/{agent_name}/{audio_format}/{b64_string}.{audio_format}"
        with open(file_name, 'rb') as file:
            # Read the entire file content into a variable
            audio_data = file.read()
    else:
        object_key = f"{assistant_id}/audio/{b64_string}.{audio_format}"
        logger.info(f"Reading {object_key}")
        audio_data = await get_s3_file(BUCKET_NAME, object_key)

    return audio_data


def get_md5_hash(text):
    return hashlib.md5(text.encode()).hexdigest()


def is_valid_md5(hash_string):
    return bool(re.fullmatch(r"[0-9a-f]{32}", hash_string))


def split_payload(payload, max_size=500 * 1024):
    if len(payload) <= max_size:
        return payload
    return [payload[i:i + max_size] for i in range(0, len(payload), max_size)]


def get_required_input_types(task):
    input_types = dict()
    for i, chain in enumerate(task['toolchain']['pipelines']):
        first_model = chain[0]
        if chain[0] == "transcriber":
            input_types["audio"] = i
        elif chain[0] == "synthesizer" or chain[0] == "llm":
            input_types["text"] = i
    return input_types


def format_messages(messages):
    formatted_string = ""
    for message in messages:
        role = message['role']
        content = message['content']

        if role == 'assistant':
            formatted_string += "assistant: " + content + "\n"
        elif role == 'user':
            formatted_string += "user: " + content + "\n"

    return formatted_string


def update_prompt_with_context(prompt, context_data):
    if not isinstance(context_data.get('recipient_data'), dict):
        return prompt
    return prompt.format_map(DictWithMissing(context_data.get('recipient_data', {})))


async def get_prompt_responses(assistant_id, local=False):
    filepath = f"{PREPROCESS_DIR}/{assistant_id}/conversation_details.json"
    data = ""
    if local:
        logger.info("Loading up the conversation details from the local file")
        try:
            with open(filepath, "r") as json_file:
                data = json.load(json_file)
        except Exception as e:
            logger.error(f"Could not load up the dataset {e}")
    else:
        key = f"{assistant_id}/conversation_details.json"
        logger.info(f"Loading up the conversation details from the s3 file BUCKET_NAME {BUCKET_NAME} {key}")
        try:
            response = await get_s3_file(BUCKET_NAME, key)
            file_content = response.decode('utf-8')
            json_content = json.loads(file_content)
            return json_content

        except Exception as e:
            traceback.print_exc()
            print(f"An error occurred: {e}")
            return None

    return data


async def execute_tasks_in_chunks(tasks, chunk_size=10):
    task_chunks = [tasks[i:i + chunk_size] for i in range(0, len(tasks), chunk_size)]

    for chunk in task_chunks:
        await asyncio.gather(*chunk)


def has_placeholders(s):
    return bool(re.search(r'\{[^{}\s]*\}', s))


def infer_type(value):
    if isinstance(value, int):
        return (int, ...)
    elif isinstance(value, float):
        return (float, ...)
    elif isinstance(value, bool):
        return (bool, ...)
    elif isinstance(value, list):
        return (list, ...)
    elif isinstance(value, dict):
        return (dict, ...)
    else:
        return (str, ...)

def json_to_pydantic_schema(json_data):
    parsed_json = json.loads(json_data)
    
    fields = {key: infer_type(value) for key, value in parsed_json.items()}
    dynamic_model = create_model('DynamicModel', **fields)
    
    return dynamic_model.schema_json(indent=2)

def clean_json_string(json_str):
    if type(json_str) is not str:
        return json_str
    if json_str.startswith("```json") and json_str.endswith("```"):
        json_str = json_str[7:-3].strip()
    return json_str

def yield_chunks_from_memory(audio_bytes, chunk_size=512):
    total_length = len(audio_bytes)
    for i in range(0, total_length, chunk_size):
        yield audio_bytes[i:i + chunk_size]

def pcm_to_wav_bytes(pcm_data, sample_rate = 16000, num_channels = 1, sample_width = 2):
    buffer = io.BytesIO()
    bit_depth = 16 
    if len(pcm_data)%2 == 1:
        pcm_data += b'\x00'
    tensor_pcm = torch.frombuffer(pcm_data, dtype=torch.int16)
    tensor_pcm = tensor_pcm.float() / (2**(bit_depth - 1))  
    tensor_pcm = tensor_pcm.unsqueeze(0)  
    torchaudio.save(buffer, tensor_pcm, sample_rate, format='wav')
    return buffer.getvalue()

def convert_audio_to_wav(audio_bytes, source_format = 'flac'):
    logger.info(f"CONVERTING AUDIO TO WAV {source_format}")
    audio = AudioSegment.from_file(io.BytesIO(audio_bytes), format=source_format)
    logger.info(f"GOT audio wav {audio}")
    buffer = io.BytesIO()
    audio.export(buffer, format="wav")
    logger.info(f"SENDING BACK WAV")
    return buffer.getvalue()

def resample(audio_bytes, target_sample_rate, format = "mp3"):
    audio_buffer = io.BytesIO(audio_bytes)
    waveform, orig_sample_rate = torchaudio.load(audio_buffer, format = format)
    if orig_sample_rate == target_sample_rate:
        return audio_bytes
    resampler = torchaudio.transforms.Resample(orig_sample_rate, target_sample_rate)
    audio_waveform = resampler(waveform)
    audio_buffer = io.BytesIO()
    logger.info(f"Resampling from {orig_sample_rate} to {target_sample_rate}")
    torchaudio.save(audio_buffer, audio_waveform, target_sample_rate, format="wav")
    return audio_buffer.getvalue()

# def merge_wav_bytes(wav_bytes_list):
#     logger.info(f"Merging {len(wav_bytes_list)} wav files")
#     if not wav_bytes_list:
#         return None
    
#     header_length = 44
#     header = wav_bytes_list[0][:header_length]
#     merged_audio_data = io.BytesIO(header)
#     data_size_offset = 40
#     total_data_size = 0

#     for wav_bytes in wav_bytes_list:
#         audio_data = wav_bytes[header_length:]
#         merged_audio_data.write(audio_data)
#         total_data_size += len(audio_data)
#     merged_audio_data.seek(data_size_offset)
#     merged_audio_data.write(total_data_size.to_bytes(4, byteorder='endian'))
#     file_size = total_data_size + header_length - 8
#     merged_audio_data.seek(4)
#     merged_audio_data.write(file_size.to_bytes(4, byteorder='endian'))
#     return merged_audio_data.getvalue()

def merge_wav_bytes(wav_files_bytes):
    combined = AudioSegment.empty()
    for wav_bytes in wav_files_bytes:
        file_like_object = io.BytesIO(wav_bytes)

        audio_segment = AudioSegment.from_file(file_like_object, format="wav")
        combined += audio_segment

    buffer = io.BytesIO()
    combined.export(buffer, format="wav")
    return buffer.getvalue()

