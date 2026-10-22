export const RETRY_MESSAGE = 'Couldn’t complete the response. Please retry.';

export function chatErrorMessage(message, status) {
  if (status >= 500 || /backend terminal|bigquery|asset service|model service/i.test(message || '')) return RETRY_MESSAGE;
  if (/failed to fetch|networkerror|network request failed/i.test(message || '')) {
    return 'Connection interrupted. Please check your connection and retry.';
  }
  return message || RETRY_MESSAGE;
}
