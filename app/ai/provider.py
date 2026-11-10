import json
from google import genai
from google.genai import types
from app.config import settings

class GeminiProvider:
    def __init__(self):
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model = settings.gemini_model

    async def generate_json(self, prompt: str) -> dict:
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
        )
        return json.loads(response.text or "{}")
