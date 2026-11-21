import io

import base64

from app.brain.client import image_part, json_call, oai, prompt
from app.brain.schemas import DocRead, ImageRead, Transcript
from app.config import settings


def transcribe(audio: bytes, filename: str = "voice.ogg") -> Transcript:
    f = io.BytesIO(audio)
    f.name = filename
    r = oai().audio.transcriptions.create(model=settings().openai_model_transcribe, file=f)
    return Transcript(text=r.text)


def read_image(data: bytes, mime: str = "image/jpeg", task_id: str | None = None) -> ImageRead:
    user = [{"type": "text", "text": "Read this image."}, image_part(data, mime)]
    return json_call(settings().openai_model_brain, prompt("read_image"), user, ImageRead, task_id, "read_image")


def tts(text: str) -> bytes:
    r = oai().audio.speech.create(model=settings().openai_model_tts, voice="alloy", input=text,
                                  instructions="Speak slowly, warmly, in simple Telugu.", response_format="mp3")
    return r.read()


def read_document(data: bytes, filename: str = "document.pdf", task_id: str | None = None) -> DocRead:
    """PDF → line items exactly as printed (the total is computed later, in code)."""
    part = {"type": "file", "file": {"filename": filename,
                                     "file_data": "data:application/pdf;base64," + base64.b64encode(data).decode()}}
    user = [{"type": "text", "text": "Read this document."}, part]
    return json_call(settings().openai_model_brain, prompt("read_document"), user, DocRead, task_id, "read_document")
