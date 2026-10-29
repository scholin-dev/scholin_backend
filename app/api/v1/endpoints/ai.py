from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import httpx
import json
import os
from ....core.session_auth import get_current_user

router = APIRouter(prefix="/ai", tags=["Ai"])

OLLAMA_URL = "http://ollama.railway.internal:11434/api/generate"
OLLAMA_MODEL = "llama3.2:1b-cpu8"

class LessonRequest(BaseModel):
    title: str
    subject: str
    class_name: str

@router.post("/generate-lesson")
async def generate_lesson(req: LessonRequest, user = Depends(get_current_user)):
    prompt = f"""
    You are an expert teacher. Create a detailed lesson plan for:
    Title: {req.title}
    Subject: {req.subject}
    Class: {req.class_name}

    CRITICAL RULES:
    1. Return ONLY a valid JSON object. Do not wrap it in markdown code blocks.
    2. Do NOT use arrays. Every value must be a single string.
    3. Use \\n to create line breaks inside the strings.
    4. Do NOT use complex LaTeX math symbols. Use plain text for math (e.g., "x squared").

    Return this exact JSON structure:
    {{
      "objectives": "1. First objective\\n2. Second objective\\n3. Third objective",
      "introduction": "A short engaging introduction... followed by a real-world example.",
      "main_lesson": "1. Step one explanation.\\n2. Step two explanation.\\n3. Include a light-hearted joke related to the topic here.\\n4. Step three explanation.",
      "practice_questions": "1. Question one?\\nAnswer: Answer one.\\n\\n2. Question two?\\nAnswer: Answer two."
    }}
    """
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json"
                }
            )
            response.raise_for_status()
            
            # Ollama wraps the response in a 'response' field
            ai_text = response.json().get("response", "{}")
            return json.loads(ai_text)
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")
