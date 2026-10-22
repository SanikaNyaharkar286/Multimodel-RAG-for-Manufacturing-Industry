// Local, deterministic safety intent detection. This runs alongside the
// backend request and never makes an extra model call.
export const GENERAL_SAFETY = [
  'Keep clear of moving parts and do not remove guards while equipment is operating.',
  'Before physical inspection or maintenance, follow your site\'s shutdown and lockout/tagout procedure. A trained, authorized person must isolate and verify all relevant energy sources.',
  'Use the PPE required by your site and the equipment manual.',
  'Wait for the machine-specific instructions before attempting the repair.',
];

const CATEGORY_RULES = [
  { pattern: /electr|wiring|voltage|circuit|breaker|power supply/i,
    text: 'Electrical work must be performed by qualified personnel.' },
  { pattern: /hydraulic|pneumatic|pressure|leak|compressed air/i,
    text: 'Stored hydraulic or pneumatic pressure must be safely released and verified by authorized personnel.' },
  { pattern: /hot|heat|overheat|temperature|burn/i,
    text: 'Allow hot components to cool before touching them.' },
  { pattern: /blade|saw|rotat|moving|jam|cutting/i,
    text: 'Wait for complete stoppage and follow the required isolation procedure before accessing blades or rotating parts.' },
];

const REPAIR_INTENT = /\b(troubleshoot|repair|fix|inspect|replace|clean|clear|check|maintenance|maintain|fault|failure|issue|problem|leak|overheat|overheating|vibrat|jam|not working|won't|stopped|pressure)\b/i;
const NAVIGATION = /^(done|next|continue|completed|issue resolved|report issue)\s*[.!?]*$/i;
const INFORMATION = /\b(what is|what does|function|used for|summarize|summary|define|meaning|specification|specifications)\b/i;
const REPAIR_CONTINUATION = /\b(continue|next step|this step|still|again|didn.t|not fixed|result|what happened|how do i)\b/i;

export function detectSafetyGuidance(question, { activeRepair = false, navigation = false } = {}) {
  const text = String(question || '').trim();
  if (!text || navigation || NAVIGATION.test(text)) return null;
  const physicalIntent = REPAIR_INTENT.test(text) && !INFORMATION.test(text);
  if (!physicalIntent && !(activeRepair && REPAIR_CONTINUATION.test(text))) return null;
  const category = CATEGORY_RULES.find(rule => rule.pattern.test(text))?.text;
  return { title: 'Safety precautions', bullets: category ? [...GENERAL_SAFETY, category] : GENERAL_SAFETY };
}
