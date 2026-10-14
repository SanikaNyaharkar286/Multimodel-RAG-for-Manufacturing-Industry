import { APP_CONFIG } from "../config/appConfig";

// Turn stored "field: value" lines into table rows
function makeTableBlocks(tables = []) {
  return tables
    .filter((item) => item && item.content)
    .map((item) => ({
      type: "table",
      title: "Manual table information",
      columns: ["Field", "Value"],
      rows: item.content
        .split("\n")
        .filter(Boolean)
        .map((line) => {
          const colon = line.indexOf(":");

          return colon >= 0
            ? [line.slice(0, colon).trim(), line.slice(colon + 1).trim()]
            : ["Information", line.trim()];
        }),
    }));
}

// Return a safe http(s) link, or null
function safeLink(link) {
  try {
    const url = new URL(link);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.href
      : null;
  } catch {
    return null;
  }
}

// Send the question and optional photo to FastAPI
export async function sendChat({ question, imageFile, sessionId }) {
  const formData = new FormData();

  formData.append("message", question || "");
  formData.append("session_id", sessionId);

  if (imageFile) {
    formData.append("image", imageFile, imageFile.name);
  }

  const response = await fetch(
    `${APP_CONFIG.apiBaseUrl}${APP_CONFIG.chatEndpoint}`,
    { method: "POST", body: formData }
  );

  if (!response.ok) {
    let message = `Server error (${response.status})`;

    try {
      const details = await response.json();
      if (typeof details.detail === "string") message = details.detail;
    } catch {
      // The error was not JSON
    }

    throw new Error(message);
  }

  const data = await response.json();

  const blocks = [
    { type: "text", content: data.reply || "No response available." },
  ];

  // Original manual images with their explanation
  for (const image of data.images || []) {
    if (image?.url) {
      blocks.push({
        type: "image",
        src: new URL(image.url, APP_CONFIG.apiBaseUrl).href,
        caption: image.description,
      });
    }
  }

  blocks.push(...makeTableBlocks(data.tables));

  for (const link of data.links || []) {
    const href = safeLink(link);
    if (href) blocks.push({ type: "link", url: href });
  }

  return { blocks };
}