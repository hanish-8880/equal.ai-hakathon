import io

from app.brain.client import image_part, json_call, oai, prompt
from app.brain.schemas import ImageRead, Transcript
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
