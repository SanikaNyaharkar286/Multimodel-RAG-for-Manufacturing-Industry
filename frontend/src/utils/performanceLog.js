// Development-only diagnostics; never print questions, images or session IDs.
export function logPerformance(event, fields) {
  if (import.meta.env.DEV) {
    console.debug(`[copilot:${event}]`, fields);
  }
}
