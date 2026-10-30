from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import httpx
import json
from ....core.session_auth import get_current_user

router = APIRouter(prefix="/ai", tags=["Ai"])

OLLAMA_URL = "http://ollama.railway.internal:11434/api/generate"
OLLAMA_MODEL = "llama3.2:1b-cpu8"

class PartRequest(BaseModel):
    title: str
    subject: str
    class_name: str
    part: str

@router.post("/generate-part")
async def generate_part(req: PartRequest, user = Depends(get_current_user)):
    # Build the instruction based on the part requested
    if req.part == 'objectives':
        instruction = 'Write exactly 3 clear learning objectives as a numbered list.'
        key = "objectives"
    elif req.part == 'introduction':
        instruction = 'Write a short, engaging introduction with one real-world example.'
        key = "introduction"
    elif req.part == 'main_lesson':
        instruction = 'Write the main lesson content step-by-step. Include at least one light-hearted joke related to the topic.'
        key = "main_lesson"
    elif req.part == 'practice_questions':
        instruction = 'Write 3 practice questions with their answers.'
        key = "practice_questions"
    else:
        raise HTTPException(status_code=400, detail="Invalid part requested")

    prompt = f"""
    You are an expert teacher. For the topic "{req.title}" ({req.subject}, {req.class_name}):
    
    INSTRUCTION: {instruction}
    
    CRITICAL RULES:
    1. Return ONLY a valid JSON object with a single key "{key}".
    2. The value must be a single string with \\n for newlines.
    3. Do NOT use complex LaTeX or math symbols. Write math in plain English.
    4. Do not include any other text besides the JSON.

    Example format:
    {{
      "{key}": "Your generated text here. Use \\n for line breaks."
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
            
            ai_text = response.json().get("response", "{}")
            data = json.loads(ai_text)
            return {"content": data.get(key, "Failed to generate.")}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")
