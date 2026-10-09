import { APP_CONFIG } from "../config/appConfig";
import { demoImageResponse, demoTextResponse } from "../data/demoResponses";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/*
  Expected backend response (JSON):
  {
    "blocks": [
      { "type": "text",    "content": "..." },
      { "type": "image",   "src": "https://... or data:image/...", "caption": "..." },
      { "type": "diagram", "src": "...", "caption": "..." },
      { "type": "table",   "title": "...", "columns": [...], "rows": [[...]] },
      { "type": "steps",   "title": "...", "items": ["...", "..."] }
    ],
    "sources": [ { "document": "...", "page": 12, "section": "..." } ]
  }
*/
export async function sendChat({ question, imageFile }) {
  // Demo mode
  if (APP_CONFIG.useDemoData) {
    await wait(1200);
    return imageFile ? demoImageResponse : demoTextResponse;
  }

  // Real backend (FastAPI)
  const formData = new FormData();
  formData.append("question", question);
  if (imageFile) {
    formData.append("image", imageFile);
  }

  const response = await fetch(
    `${APP_CONFIG.apiBaseUrl}${APP_CONFIG.chatEndpoint}`,
    { method: "POST", body: formData }
  );

  if (!response.ok) {
    throw new Error(`Server error (${response.status}). Please try again.`);
  }

  return response.json();
}