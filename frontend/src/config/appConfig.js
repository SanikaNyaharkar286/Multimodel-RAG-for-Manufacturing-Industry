export const APP_CONFIG = {
  appName: "Manufacturing Equipment Copilot",

  // FastAPI backend
  apiBaseUrl: import.meta.env.VITE_API_URL || "http://127.0.0.1:8001",
  chatEndpoint: "/chat",

  // Image upload limits
  maxImageSizeMB: 5,
  allowedImageTypes: ["image/jpeg", "image/png", "image/webp"],

  // Example questions on the welcome screen
  exampleQuestions: [
    "What is this component?",
    "How to fix this issue?",
    "Show manual diagram",
    "What is error 1001?",
  ],
};