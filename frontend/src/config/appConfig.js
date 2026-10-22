export const APP_CONFIG = {
  appName: "Manufacturing Equipment Copilot",

  apiBaseUrl: (
    import.meta.env.VITE_API_URL ||
    "http://127.0.0.1:8001"
  ).replace(/\/$/, ""),

  chatEndpoint: "/chat",

  maxImageSizeMB: 5,

  allowedImageTypes: [
    "image/jpeg",
    "image/png",
    "image/webp",
  ],

  exampleQuestions: [
    "What are the main functions of the control panel?",
    "Why is my grinding wheel vibrating?",
    "Show the control panel diagram.",
    "Guide me through troubleshooting one step at a time.",
  ],
};
