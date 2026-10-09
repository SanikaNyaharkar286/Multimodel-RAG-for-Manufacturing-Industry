import { APP_CONFIG } from "../config/appConfig";

export function validateImage(file) {
  if (!file) {
    return { valid: false, error: "No file selected." };
  }

  if (!APP_CONFIG.allowedImageTypes.includes(file.type)) {
    return { valid: false, error: "Please upload a JPG, PNG or WEBP image." };
  }

  const maxBytes = APP_CONFIG.maxImageSizeMB * 1024 * 1024;
  if (file.size > maxBytes) {
    return {
      valid: false,
      error: `Image must be less than ${APP_CONFIG.maxImageSizeMB} MB.`,
    };
  }

  return { valid: true, error: "" };
}