import hashlib
import json
import logging
import os
import uuid
from functools import lru_cache
from pathlib import Path
from pydub import AudioSegment
from pydub.silence import split_on_silence
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fnmatch import fnmatch
import aiohttp
import aiofiles
import requests
import mimetypes

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
    APIRouter,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.config import (
    WHISPER_MODEL_AUTO_UPDATE,
    WHISPER_MODEL_DIR,
    CACHE_DIR,
    WHISPER_LANGUAGE,
)

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT,
    ENV,
    SRC_LOG_LEVELS,
    DEVICE_TYPE,
    ENABLE_FORWARD_USER_INFO_HEADERS,
)


router = APIRouter()

# Constants
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024  # Convert MB to bytes
AZURE_MAX_FILE_SIZE_MB = 200
AZURE_MAX_FILE_SIZE = AZURE_MAX_FILE_SIZE_MB * 1024 * 1024  # Convert MB to bytes

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["AUDIO"])

SPEECH_CACHE_DIR = CACHE_DIR / "audio" / "speech"
SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


##########################################
#
# Utility functions
#
##########################################

from pydub import AudioSegment
from pydub.utils import mediainfo


def is_audio_conversion_required(file_path):
    """
    Check if the given audio file needs conversion to mp3.
    """
    SUPPORTED_FORMATS = {"flac", "m4a", "mp3", "mp4", "mpeg", "wav", "webm"}

    if not os.path.isfile(file_path):
        log.error(f"File not found: {file_path}")
        return False

    try:
        info = mediainfo(file_path)
        codec_name = info.get("codec_name", "").lower()
        codec_type = info.get("codec_type", "").lower()
        codec_tag_string = info.get("codec_tag_string", "").lower()

        if codec_name == "aac" and codec_type == "audio" and codec_tag_string == "mp4a":
            # File is AAC/mp4a audio, recommend mp3 conversion
            return True

        # If the codec name is in the supported formats
        if codec_name in SUPPORTED_FORMATS:
            return False

        return True
    except Exception as e:
        log.error(f"Error getting audio format: {e}")
        return False


def convert_audio_to_mp3(file_path):
    """Convert audio file to mp3 format."""
    try:
        output_path = os.path.splitext(file_path)[0] + ".mp3"
        audio = AudioSegment.from_file(file_path)
        audio.export(output_path, format="mp3")
        log.info(f"Converted {file_path} to {output_path}")
        return output_path
    except Exception as e:
        log.error(f"Error converting audio file: {e}")
        return None


def set_faster_whisper_model(model: str, auto_update: bool = False):
    whisper_model = None
    if model:
        from faster_whisper import WhisperModel

        faster_whisper_kwargs = {
            "model_size_or_path": model,
            "device": DEVICE_TYPE if DEVICE_TYPE and DEVICE_TYPE == "cuda" else "cpu",
            "compute_type": "int8",
            "download_root": WHISPER_MODEL_DIR,
            "local_files_only": not auto_update,
        }

        try:
            whisper_model = WhisperModel(**faster_whisper_kwargs)
        except Exception:
            log.warning(
                "WhisperModel initialization failed, attempting download with local_files_only=False"
            )
            faster_whisper_kwargs["local_files_only"] = False
            whisper_model = WhisperModel(**faster_whisper_kwargs)
    return whisper_model


##########################################
#
# Audio API
#
##########################################


class TTSConfigForm(BaseModel):
    OPENAI_API_BASE_URL: str
    OPENAI_API_KEY: str
    API_KEY: str
    ENGINE: str
    MODEL: str
    VOICE: str
    SPLIT_ON: str
    AZURE_SPEECH_REGION: str
    AZURE_SPEECH_BASE_URL: str
    AZURE_SPEECH_OUTPUT_FORMAT: str


class STTConfigForm(BaseModel):
    OPENAI_API_BASE_URL: str
    OPENAI_API_KEY: str
    ENGINE: str
    MODEL: str
    SUPPORTED_CONTENT_TYPES: list[str] = []
    WHISPER_MODEL: str
    DEEPGRAM_API_KEY: str
    AZURE_API_KEY: str
    AZURE_REGION: str
    AZURE_LOCALES: str
    AZURE_BASE_URL: str
    AZURE_MAX_SPEAKERS: str


class AudioConfigUpdateForm(BaseModel):
    tts: TTSConfigForm
    stt: STTConfigForm


@router.get("/config")
async def get_audio_config(request: Request, user=Depends(get_admin_user)):
    return {
        "tts": {
            "OPENAI_API_BASE_URL": request.app.state.config.TTS_OPENAI_API_BASE_URL,
            "OPENAI_API_KEY": request.app.state.config.TTS_OPENAI_API_KEY,
            "API_KEY": request.app.state.config.TTS_API_KEY,
            "ENGINE": request.app.state.config.TTS_ENGINE,
            "MODEL": request.app.state.config.TTS_MODEL,
            "VOICE": request.app.state.config.TTS_VOICE,
            "SPLIT_ON": request.app.state.config.TTS_SPLIT_ON,
            "AZURE_SPEECH_REGION": request.app.state.config.TTS_AZURE_SPEECH_REGION,
            "AZURE_SPEECH_BASE_URL": request.app.state.config.TTS_AZURE_SPEECH_BASE_URL,
            "AZURE_SPEECH_OUTPUT_FORMAT": request.app.state.config.TTS_AZURE_SPEECH_OUTPUT_FORMAT,
        },
        "stt": {
            "OPENAI_API_BASE_URL": request.app.state.config.STT_OPENAI_API_BASE_URL,
            "OPENAI_API_KEY": request.app.state.config.STT_OPENAI_API_KEY,
            "ENGINE": request.app.state.config.STT_ENGINE,
            "MODEL": request.app.state.config.STT_MODEL,
            "SUPPORTED_CONTENT_TYPES": request.app.state.config.STT_SUPPORTED_CONTENT_TYPES,
            "WHISPER_MODEL": request.app.state.config.WHISPER_MODEL,
            "DEEPGRAM_API_KEY": request.app.state.config.DEEPGRAM_API_KEY,
            "AZURE_API_KEY": request.app.state.config.AUDIO_STT_AZURE_API_KEY,
            "AZURE_REGION": request.app.state.config.AUDIO_STT_AZURE_REGION,
            "AZURE_LOCALES": request.app.state.config.AUDIO_STT_AZURE_LOCALES,
            "AZURE_BASE_URL": request.app.state.config.AUDIO_STT_AZURE_BASE_URL,
            "AZURE_MAX_SPEAKERS": request.app.state.config.AUDIO_STT_AZURE_MAX_SPEAKERS,
        },
    }


@router.post("/config/update")
async def update_audio_config(
    request: Request, form_data: AudioConfigUpdateForm, user=Depends(get_admin_user)
):
    request.app.state.config.TTS_OPENAI_API_BASE_URL = form_data.tts.OPENAI_API_BASE_URL
    request.app.state.config.TTS_OPENAI_API_KEY = form_data.tts.OPENAI_API_KEY
    request.app.state.config.TTS_API_KEY = form_data.tts.API_KEY
    request.app.state.config.TTS_ENGINE = form_data.tts.ENGINE
    request.app.state.config.TTS_MODEL = form_data.tts.MODEL
    request.app.state.config.TTS_VOICE = form_data.tts.VOICE
    request.app.state.config.TTS_SPLIT_ON = form_data.tts.SPLIT_ON
    request.app.state.config.TTS_AZURE_SPEECH_REGION = form_data.tts.AZURE_SPEECH_REGION
    request.app.state.config.TTS_AZURE_SPEECH_BASE_URL = (
        form_data.tts.AZURE_SPEECH_BASE_URL
    )
    request.app.state.config.TTS_AZURE_SPEECH_OUTPUT_FORMAT = (
        form_data.tts.AZURE_SPEECH_OUTPUT_FORMAT
    )

    request.app.state.config.STT_OPENAI_API_BASE_URL = form_data.stt.OPENAI_API_BASE_URL
    request.app.state.config.STT_OPENAI_API_KEY = form_data.stt.OPENAI_API_KEY
    request.app.state.config.STT_ENGINE = form_data.stt.ENGINE
    request.app.state.config.STT_MODEL = form_data.stt.MODEL
    request.app.state.config.STT_SUPPORTED_CONTENT_TYPES = (
        form_data.stt.SUPPORTED_CONTENT_TYPES
    )

    request.app.state.config.WHISPER_MODEL = form_data.stt.WHISPER_MODEL
    request.app.state.config.DEEPGRAM_API_KEY = form_data.stt.DEEPGRAM_API_KEY
    request.app.state.config.AUDIO_STT_AZURE_API_KEY = form_data.stt.AZURE_API_KEY
    request.app.state.config.AUDIO_STT_AZURE_REGION = form_data.stt.AZURE_REGION
    request.app.state.config.AUDIO_STT_AZURE_LOCALES = form_data.stt.AZURE_LOCALES
    request.app.state.config.AUDIO_STT_AZURE_BASE_URL = form_data.stt.AZURE_BASE_URL
    request.app.state.config.AUDIO_STT_AZURE_MAX_SPEAKERS = (
        form_data.stt.AZURE_MAX_SPEAKERS
    )

    if request.app.state.config.STT_ENGINE == "":
        request.app.state.faster_whisper_model = set_faster_whisper_model(
            form_data.stt.WHISPER_MODEL, WHISPER_MODEL_AUTO_UPDATE
        )
    else:
        request.app.state.faster_whisper_model = None

    return {
        "tts": {
            "OPENAI_API_BASE_URL": request.app.state.config.TTS_OPENAI_API_BASE_URL,
            "OPENAI_API_KEY": request.app.state.config.TTS_OPENAI_API_KEY,
            "API_KEY": request.app.state.config.TTS_API_KEY,
            "ENGINE": request.app.state.config.TTS_ENGINE,
            "MODEL": request.app.state.config.TTS_MODEL,
            "VOICE": request.app.state.config.TTS_VOICE,
            "SPLIT_ON": request.app.state.config.TTS_SPLIT_ON,
            "AZURE_SPEECH_REGION": request.app.state.config.TTS_AZURE_SPEECH_REGION,
            "AZURE_SPEECH_BASE_URL": request.app.state.config.TTS_AZURE_SPEECH_BASE_URL,
            "AZURE_SPEECH_OUTPUT_FORMAT": request.app.state.config.TTS_AZURE_SPEECH_OUTPUT_FORMAT,
        },
        "stt": {
            "OPENAI_API_BASE_URL": request.app.state.config.STT_OPENAI_API_BASE_URL,
            "OPENAI_API_KEY": request.app.state.config.STT_OPENAI_API_KEY,
            "ENGINE": request.app.state.config.STT_ENGINE,
            "MODEL": request.app.state.config.STT_MODEL,
            "SUPPORTED_CONTENT_TYPES": request.app.state.config.STT_SUPPORTED_CONTENT_TYPES,
            "WHISPER_MODEL": request.app.state.config.WHISPER_MODEL,
            "DEEPGRAM_API_KEY": request.app.state.config.DEEPGRAM_API_KEY,
            "AZURE_API_KEY": request.app.state.config.AUDIO_STT_AZURE_API_KEY,
            "AZURE_REGION": request.app.state.config.AUDIO_STT_AZURE_REGION,
            "AZURE_LOCALES": request.app.state.config.AUDIO_STT_AZURE_LOCALES,
            "AZURE_BASE_URL": request.app.state.config.AUDIO_STT_AZURE_BASE_URL,
            "AZURE_MAX_SPEAKERS": request.app.state.config.AUDIO_STT_AZURE_MAX_SPEAKERS,
        },
    }


def load_speech_pipeline(request):
    from transformers import pipeline
    from datasets import load_dataset

    if request.app.state.speech_synthesiser is None:
        request.app.state.speech_synthesiser = pipeline(
            "text-to-speech", "microsoft/speecht5_tts"
        )

    if request.app.state.speech_speaker_embeddings_dataset is None:
        request.app.state.speech_speaker_embeddings_dataset = load_dataset(
            "Matthijs/cmu-arctic-xvectors", split="validation"
        )


@router.post("/speech")
async def speech(request: Request, user=Depends(get_verified_user)):
    body = await request.body()
    name = hashlib.sha256(
        body
        + str(request.app.state.config.TTS_ENGINE).encode("utf-8")
        + str(request.app.state.config.TTS_MODEL).encode("utf-8")
    ).hexdigest()

    file_path = SPEECH_CACHE_DIR.joinpath(f"{name}.mp3")
    file_body_path = SPEECH_CACHE_DIR.joinpath(f"{name}.json")

    # Check if the file already exists in the cache
    if file_path.is_file():
        return FileResponse(file_path)

    payload = None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if request.app.state.config.TTS_ENGINE == "openai":
        payload["model"] = request.app.state.config.TTS_MODEL

        try:
            timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as session:
                async with session.post(
                    url=f"{request.app.state.config.TTS_OPENAI_API_BASE_URL}/audio/speech",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {request.app.state.config.TTS_OPENAI_API_KEY}",
                        **(
                            {
                                "X-OpenWebUI-User-Name": user.name,
                                "X-OpenWebUI-User-Id": user.id,
                                "X-OpenWebUI-User-Email": user.email,
                                "X-OpenWebUI-User-Role": user.role,
                            }
                            if ENABLE_FORWARD_USER_INFO_HEADERS
                            else {}
                        ),
                    },
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    r.raise_for_status()

                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(await r.read())

                    async with aiofiles.open(file_body_path, "w") as f:
                        await f.write(json.dumps(payload))

            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)
            detail = None

            try:
                if r.status != 200:
                    res = await r.json()

                    if "error" in res:
                        detail = f"External: {res['error'].get('message', '')}"
            except Exception:
                detail = f"External: {e}"

            raise HTTPException(
                status_code=getattr(r, "status", 500) if r else 500,
                detail=detail if detail else "AI Inonia: Server Connection Error",
            )

    elif request.app.state.config.TTS_ENGINE == "elevenlabs":
        voice_id = payload.get("voice", "")

        if voice_id not in get_available_voices(request):
            raise HTTPException(
                status_code=400,
                detail="Invalid voice id",
            )

        try:
            timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as session:
                async with session.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    json={
                        "text": payload["input"],
                        "model_id": request.app.state.config.TTS_MODEL,
                        "voice_settings": {"stability": 0.5, "similarity_boost": 0.5},
                    },
                    headers={
                        "Accept": "audio/mpeg",
                        "Content-Type": "application/json",
                        "xi-api-key": request.app.state.config.TTS_API_KEY,
                    },
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    r.raise_for_status()

                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(await r.read())

                    async with aiofiles.open(file_body_path, "w") as f:
                        await f.write(json.dumps(payload))

            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)
            detail = None

            try:
                if r.status != 200:
                    res = await r.json()
                    if "error" in res:
                        detail = f"External: {res['error'].get('message', '')}"
            except Exception:
                detail = f"External: {e}"

            raise HTTPException(
                status_code=getattr(r, "status", 500) if r else 500,
                detail=detail if detail else "AI Inonia: Server Connection Error",
            )

    elif request.app.state.config.TTS_ENGINE == "azure":
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            log.exception(e)
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        region = request.app.state.config.TTS_AZURE_SPEECH_REGION or "eastus"
        base_url = request.app.state.config.TTS_AZURE_SPEECH_BASE_URL
        language = request.app.state.config.TTS_VOICE
        locale = "-".join(request.app.state.config.TTS_VOICE.split("-")[:1])
        output_format = request.app.state.config.TTS_AZURE_SPEECH_OUTPUT_FORMAT

        try:
            data = f"""<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{locale}">
                <voice name="{language}">{payload["input"]}</voice>
            </speak>"""
            timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as session:
                async with session.post(
                    (base_url or f"https://{region}.tts.speech.microsoft.com")
                    + "/cognitiveservices/v1",
                    headers={
                        "Ocp-Apim-Subscription-Key": request.app.state.config.TTS_API_KEY,
                        "Content-Type": "application/ssml+xml",
                        "X-Microsoft-OutputFormat": output_format,
                    },
                    data=data,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    r.raise_for_status()

                    async with aiofiles.open(file_path, "wb") as f:
                        await f.write(await r.read())

                    async with aiofiles.open(file_body_path, "w") as f:
                        await f.write(json.dumps(payload))

                    return FileResponse(file_path)

        except Exception as e:
            log.exception(e)
            detail = None

            try:
                if r.status != 200:
                    res = await r.json()
                    if "error" in res:
                        detail = f"External: {res['error'].get('message', '')}"
            except Exception:
                detail = f"External: {e}"

            raise HTTPException(
                status_code=getattr(r, "status", 500) if r else 500,
                detail=detail if detail else "AI Inonia: Server Connection Error",
            )

    elif request.app.state.config.TTS_ENGINE == "transformers":
        payload = None
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            log.exception(e)
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        import torch
        import soundfile as sf

        load_speech_pipeline(request)

        embeddings_dataset = request.app.state.speech_speaker_embeddings_dataset

        speaker_index = 6799
        try:
            speaker_index = embeddings_dataset["filename"].index(
                request.app.state.config.TTS_MODEL
            )
        except Exception:
            pass

        speaker_embedding = torch.tensor(
            embeddings_dataset[speaker_index]["xvector"]
        ).unsqueeze(0)

        speech = request.app.state.speech_synthesiser(
            payload["input"],
            forward_params={"speaker_embeddings": speaker_embedding},
        )

        sf.write(file_path, speech["audio"], samplerate=speech["sampling_rate"])

        async with aiofiles.open(file_body_path, "w") as f:
            await f.write(json.dumps(payload))

        return FileResponse(file_path)


def transcription_handler(request, file_path, metadata):
    filename = os.path.basename(file_path)
    file_dir = os.path.dirname(file_path)
    id = filename.split(".")[0]

    metadata = metadata or {}

    if request.app.state.config.STT_ENGINE == "":
        if request.app.state.faster_whisper_model is None:
            request.app.state.faster_whisper_model = set_faster_whisper_model(
                request.app.state.config.WHISPER_MODEL
            )

        model = request.app.state.faster_whisper_model
        segments, info = model.transcribe(
            file_path,
            beam_size=5,
            vad_filter=request.app.state.config.WHISPER_VAD_FILTER,
            language=metadata.get("language") or WHISPER_LANGUAGE,
        )
        log.info(
            "Detected language '%s' with probability %f"
            % (info.language, info.language_probability)
        )

        transcript = "".join([segment.text for segment in list(segments)])
        data = {"text": transcript.strip()}

        # save the transcript to a json file
        transcript_file = f"{file_dir}/{id}.json"
        with open(transcript_file, "w") as f:
            json.dump(data, f)

        log.debug(data)
        return data
    elif request.app.state.config.STT_ENGINE == "openai":
        r = None
        try:
            r = requests.post(
                url=f"{request.app.state.config.STT_OPENAI_API_BASE_URL}/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {request.app.state.config.STT_OPENAI_API_KEY}"
                },
                files={"file": (filename, open(file_path, "rb"))},
                data={
                    "model": request.app.state.config.STT_MODEL,
                    **(
                        {"language": metadata.get("language")}
                        if metadata.get("language")
                        else {}
                    ),
                },
            )

            r.raise_for_status()
            data = r.json()

            # save the transcript to a json file
            transcript_file = f"{file_dir}/{id}.json"
            with open(transcript_file, "w") as f:
                json.dump(data, f)

            return data
        except Exception as e:
            log.exception(e)

            detail = None
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        detail = f"External: {res['error'].get('message', '')}"
                except Exception:
                    detail = f"External: {e}"

            raise Exception(detail if detail else "AI Inonia: Server Connection Error")

    elif request.app.state.config.STT_ENGINE == "deepgram":
        try:
            # Determine the MIME type of the file
            mime, _ = mimetypes.guess_type(file_path)
            if not mime:
                mime = "audio/wav"  # fallback to wav if undetectable

            # Read the audio file
            with open(file_path, "rb") as f:
                file_data = f.read()

            # Build headers and parameters
            headers = {
                "Authorization": f"Token {request.app.state.config.DEEPGRAM_API_KEY}",
                "Content-Type": mime,
            }

            # Add model if specified
            params = {}
            if request.app.state.config.STT_MODEL:
                params["model"] = request.app.state.config.STT_MODEL

            # Make request to Deepgram API
            r = requests.post(
                "https://api.deepgram.com/v1/listen?smart_format=true",
                headers=headers,
                params=params,
                data=file_data,
            )
            r.raise_for_status()
            response_data = r.json()

            # Extract transcript from Deepgram response
            try:
                transcript = response_data["results"]["channels"][0]["alternatives"][
                    0
                ].get("transcript", "")
            except (KeyError, IndexError) as e:
                log.error(f"Malformed response from Deepgram: {str(e)}")
                raise Exception(
                    "Failed to parse Deepgram response - unexpected response format"
                )
            data = {"text": transcript.strip()}

            # Save transcript
            transcript_file = f"{file_dir}/{id}.json"
            with open(transcript_file, "w") as f:
                json.dump(data, f)

            return data

        except Exception as e:
            log.exception(e)
            detail = None
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        detail = f"External: {res['error'].get('message', '')}"
                except Exception:
                    detail = f"External: {e}"
            raise Exception(detail if detail else "AI Inonia: Server Connection Error")

    elif request.app.state.config.STT_ENGINE == "azure":
        # Check file exists and size
        if not os.path.exists(file_path):
            raise HTTPException(status_code=400, detail="Audio file not found")

        # Check file size (Azure has a larger limit of 200MB)
        file_size = os.path.getsize(file_path)
        if file_size > AZURE_MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File size exceeds Azure's limit of {AZURE_MAX_FILE_SIZE_MB}MB",
            )

        api_key = request.app.state.config.AUDIO_STT_AZURE_API_KEY
        region = request.app.state.config.AUDIO_STT_AZURE_REGION or "eastus"
        locales = request.app.state.config.AUDIO_STT_AZURE_LOCALES
        base_url = request.app.state.config.AUDIO_STT_AZURE_BASE_URL
        max_speakers = request.app.state.config.AUDIO_STT_AZURE_MAX_SPEAKERS or 3

        # IF NO LOCALES, USE DEFAULTS
        if len(locales) < 2:
            locales = [
                "en-US",
                "es-ES",
                "es-MX",
                "fr-FR",
                "hi-IN",
                "it-IT",
                "de-DE",
                "en-GB",
                "en-IN",
                "ja-JP",
                "ko-KR",
                "pt-BR",
                "zh-CN",
            ]
            locales = ",".join(locales)

        if not api_key or not region:
            raise HTTPException(
                status_code=400,
                detail="Azure API key is required for Azure STT",
            )

        r = None
        try:
            # Prepare the request
            data = {
                "definition": json.dumps(
                    {
                        "locales": locales.split(","),
                        "diarization": {"maxSpeakers": max_speakers, "enabled": True},
                    }
                    if locales
                    else {}
                )
            }

            url = (
                base_url or f"https://{region}.api.cognitive.microsoft.com"
            ) + "/speechtotext/transcriptions:transcribe?api-version=2024-11-15"

            # Use context manager to ensure file is properly closed
            with open(file_path, "rb") as audio_file:
                r = requests.post(
                    url=url,
                    files={"audio": audio_file},
                    data=data,
                    headers={
                        "Ocp-Apim-Subscription-Key": api_key,
                    },
                )

            r.raise_for_status()
            response = r.json()

            # Extract transcript from response
            if not response.get("combinedPhrases"):
                raise ValueError("No transcription found in response")

            # Get the full transcript from combinedPhrases
            transcript = response["combinedPhrases"][0].get("text", "").strip()
            if not transcript:
                raise ValueError("Empty transcript in response")

            data = {"text": transcript}

            # Save transcript to json file (consistent with other providers)
            transcript_file = f"{file_dir}/{id}.json"
            with open(transcript_file, "w") as f:
                json.dump(data, f)

            log.debug(data)
            return data

        except (KeyError, IndexError, ValueError) as e:
            log.exception("Error parsing Azure response")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse Azure response: {str(e)}",
            )
        except requests.exceptions.RequestException as e:
            log.exception(e)
            detail = None

            try:
                if r is not None and r.status_code != 200:
                    res = r.json()
                    if "error" in res:
                        detail = f"External: {res['error'].get('message', '')}"
            except Exception:
                detail = f"External: {e}"

            raise HTTPException(
                status_code=getattr(r, "status_code", 500) if r else 500,
                detail=detail if detail else "AI Inonia: Server Connection Error",
            )


def transcribe(request: Request, file_path: str, metadata: Optional[dict] = None):
    log.info(f"transcribe: {file_path} {metadata}")

    if is_audio_conversion_required(file_path):
        file_path = convert_audio_to_mp3(file_path)

    try:
        file_path = compress_audio(file_path)
    except Exception as e:
        log.exception(e)

    # Always produce a list of chunk paths (could be one entry if small)
    try:
        chunk_paths = split_audio(file_path, MAX_FILE_SIZE)
        print(f"Chunk paths: {chunk_paths}")
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )

    results = []
    try:
        with ThreadPoolExecutor() as executor:
            # Submit tasks for each chunk_path
            futures = [
                executor.submit(transcription_handler, request, chunk_path, metadata)
                for chunk_path in chunk_paths
            ]
            # Gather results as they complete
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as transcribe_exc:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Error transcribing chunk: {transcribe_exc}",
                    )
    finally:
        # Clean up only the temporary chunks, never the original file
        for chunk_path in chunk_paths:
            if chunk_path != file_path and os.path.isfile(chunk_path):
                try:
                    os.remove(chunk_path)
                except Exception:
                    pass

    return {
        "text": " ".join([result["text"] for result in results]),
    }


def compress_audio(file_path):
    if os.path.getsize(file_path) > MAX_FILE_SIZE:
        id = os.path.splitext(os.path.basename(file_path))[
            0
        ]  # Handles names with multiple dots
        file_dir = os.path.dirname(file_path)

        audio = AudioSegment.from_file(file_path)
        audio = audio.set_frame_rate(16000).set_channels(1)  # Compress audio

        compressed_path = os.path.join(file_dir, f"{id}_compressed.mp3")
        audio.export(compressed_path, format="mp3", bitrate="32k")
        # log.debug(f"Compressed audio to {compressed_path}")  # Uncomment if log is defined

        return compressed_path
    else:
        return file_path


def split_audio(file_path, max_bytes, format="mp3", bitrate="32k"):
    """
    Splits audio into chunks not exceeding max_bytes.
    Returns a list of chunk file paths. If audio fits, returns list with original path.
    """
    file_size = os.path.getsize(file_path)
    if file_size <= max_bytes:
        return [file_path]  # Nothing to split

    audio = AudioSegment.from_file(file_path)
    duration_ms = len(audio)
    orig_size = file_size

    approx_chunk_ms = max(int(duration_ms * (max_bytes / orig_size)) - 1000, 1000)
    chunks = []
    start = 0
    i = 0

    base, _ = os.path.splitext(file_path)

    while start < duration_ms:
        end = min(start + approx_chunk_ms, duration_ms)
        chunk = audio[start:end]
        chunk_path = f"{base}_chunk_{i}.{format}"
        chunk.export(chunk_path, format=format, bitrate=bitrate)

        # Reduce chunk duration if still too large
        while os.path.getsize(chunk_path) > max_bytes and (end - start) > 5000:
            end = start + ((end - start) // 2)
            chunk = audio[start:end]
            chunk.export(chunk_path, format=format, bitrate=bitrate)

        if os.path.getsize(chunk_path) > max_bytes:
            os.remove(chunk_path)
            raise Exception("Audio chunk cannot be reduced below max file size.")

        chunks.append(chunk_path)
        start = end
        i += 1

    return chunks


@router.post("/transcriptions")
def transcription(
    request: Request,
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    user=Depends(get_verified_user),
):
    log.info(f"file.content_type: {file.content_type}")

    supported_content_types = request.app.state.config.STT_SUPPORTED_CONTENT_TYPES or [
        "audio/*",
        "video/webm",
    ]

    if not any(
        fnmatch(file.content_type, content_type)
        for content_type in supported_content_types
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.FILE_NOT_SUPPORTED,
        )

    try:
        ext = file.filename.split(".")[-1]
        id = uuid.uuid4()

        filename = f"{id}.{ext}"
        contents = file.file.read()

        file_dir = f"{CACHE_DIR}/audio/transcriptions"
        os.makedirs(file_dir, exist_ok=True)
        file_path = f"{file_dir}/{filename}"

        with open(file_path, "wb") as f:
            f.write(contents)

        try:
            metadata = None

            if language:
                metadata = {"language": language}

            result = transcribe(request, file_path, metadata)

            return {
                **result,
                "filename": os.path.basename(file_path),
            }

        except Exception as e:
            log.exception(e)

            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ERROR_MESSAGES.DEFAULT(e),
            )

    except Exception as e:
        log.exception(e)

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ERROR_MESSAGES.DEFAULT(e),
        )


def get_available_models(request: Request) -> list[dict]:
    available_models = []
    if request.app.state.config.TTS_ENGINE == "openai":
        # Use custom endpoint if not using the official OpenAI API URL
        if not request.app.state.config.TTS_OPENAI_API_BASE_URL.startswith(
            "https://api.openai.com"
        ):
            try:
                response = requests.get(
                    f"{request.app.state.config.TTS_OPENAI_API_BASE_URL}/audio/models"
                )
                response.raise_for_status()
                data = response.json()
                available_models = data.get("models", [])
            except Exception as e:
                log.error(f"Error fetching models from custom endpoint: {str(e)}")
                available_models = [{"id": "tts-1"}, {"id": "tts-1-hd"}]
        else:
            available_models = [{"id": "tts-1"}, {"id": "tts-1-hd"}]
    elif request.app.state.config.TTS_ENGINE == "elevenlabs":
        try:
            response = requests.get(
                "https://api.elevenlabs.io/v1/models",
                headers={
                    "xi-api-key": request.app.state.config.TTS_API_KEY,
                    "Content-Type": "application/json",
                },
                timeout=5,
            )
            response.raise_for_status()
            models = response.json()

            available_models = [
                {"name": model["name"], "id": model["model_id"]} for model in models
            ]
        except requests.RequestException as e:
            log.error(f"Error fetching voices: {str(e)}")
    return available_models


@router.get("/models")
async def get_models(request: Request, user=Depends(get_verified_user)):
    return {"models": get_available_models(request)}


def get_available_voices(request) -> dict:
    """Returns {voice_id: voice_name} dict"""
    available_voices = {}
    if request.app.state.config.TTS_ENGINE == "openai":
        # Use custom endpoint if not using the official OpenAI API URL
        if not request.app.state.config.TTS_OPENAI_API_BASE_URL.startswith(
            "https://api.openai.com"
        ):
            try:
                response = requests.get(
                    f"{request.app.state.config.TTS_OPENAI_API_BASE_URL}/audio/voices"
                )
                response.raise_for_status()
                data = response.json()
                voices_list = data.get("voices", [])
                available_voices = {voice["id"]: voice["name"] for voice in voices_list}
            except Exception as e:
                log.error(f"Error fetching voices from custom endpoint: {str(e)}")
                available_voices = {
                    "alloy": "alloy",
                    "echo": "echo",
                    "fable": "fable",
                    "onyx": "onyx",
                    "nova": "nova",
                    "shimmer": "shimmer",
                }
        else:
            available_voices = {
                "alloy": "alloy",
                "echo": "echo",
                "fable": "fable",
                "onyx": "onyx",
                "nova": "nova",
                "shimmer": "shimmer",
            }
    elif request.app.state.config.TTS_ENGINE == "elevenlabs":
        try:
            available_voices = get_elevenlabs_voices(
                api_key=request.app.state.config.TTS_API_KEY
            )
        except Exception:
            # Avoided @lru_cache with exception
            pass
    elif request.app.state.config.TTS_ENGINE == "azure":
        try:
            region = request.app.state.config.TTS_AZURE_SPEECH_REGION
            base_url = request.app.state.config.TTS_AZURE_SPEECH_BASE_URL
            url = (
                base_url or f"https://{region}.tts.speech.microsoft.com"
            ) + "/cognitiveservices/voices/list"
            headers = {
                "Ocp-Apim-Subscription-Key": request.app.state.config.TTS_API_KEY
            }

            response = requests.get(url, headers=headers)
            response.raise_for_status()
            voices = response.json()

            for voice in voices:
                available_voices[voice["ShortName"]] = (
                    f"{voice['DisplayName']} ({voice['ShortName']})"
                )
        except requests.RequestException as e:
            log.error(f"Error fetching voices: {str(e)}")

    return available_voices


@lru_cache
def get_elevenlabs_voices(api_key: str) -> dict:
    """
    Note, set the following in your .env file to use Elevenlabs:
    AUDIO_TTS_ENGINE=elevenlabs
    AUDIO_TTS_API_KEY=sk_...  # Your Elevenlabs API key
    AUDIO_TTS_VOICE=EXAVITQu4vr4xnSDxMaL  # From https://api.elevenlabs.io/v1/voices
    AUDIO_TTS_MODEL=eleven_multilingual_v2
    """

    try:
        # TODO: Add retries
        response = requests.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        voices_data = response.json()

        voices = {}
        for voice in voices_data.get("voices", []):
            voices[voice["voice_id"]] = voice["name"]
    except requests.RequestException as e:
        # Avoid @lru_cache with exception
        log.error(f"Error fetching voices: {str(e)}")
        raise RuntimeError(f"Error fetching voices: {str(e)}")

    return voices


@router.get("/voices")
async def get_voices(request: Request, user=Depends(get_verified_user)):
    return {
        "voices": [
            {"id": k, "name": v} for k, v in get_available_voices(request).items()
        ]
    }
