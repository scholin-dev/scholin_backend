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

    Return the response in this exact JSON format with no extra text or markdown formatting:
    {{
      "objectives": 
        "1. ...
        2. ...
        3. ...",
      "introduction": "A short engaging introduction...followed by example",
      "main_lesson": "Detailed steps by step and jokes also related to topic, explanations, and examples...list them in numbers",
      "practice_questions": "3-5 practice questions with answers..."
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
