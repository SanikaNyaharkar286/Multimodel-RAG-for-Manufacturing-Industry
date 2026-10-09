export const APP_CONFIG = {
  appName: "Manufacturing Equipment Copilot",

  // true  = show sample responses (no backend needed)
  // false = call the FastAPI backend
  useDemoData: true,

  apiBaseUrl: import.meta.env.VITE_API_URL || "http://localhost:8000",
  chatEndpoint: "/api/chat",

  maxImageSizeMB: 5,
  allowedImageTypes: ["image/jpeg", "image/png", "image/webp"],

  exampleQuestions: [
    "What is this component?",
    "How to fix this issue?",
    "Show manual diagram",
    "What is error 1001?",
  ],
};