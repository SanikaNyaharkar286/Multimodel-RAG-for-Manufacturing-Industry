const LABELS = {
  approved: "Approved",
  pending: "Pending",
  rejected: "Rejected",
  suspended: "Suspended",
};

export function profileName(profile) {
  return profile?.name || profile?.email || "User";
}

export function displayLabel(value) {
  if (!value) return "";
  return String(value).charAt(0).toUpperCase() + String(value).slice(1).toLowerCase();
}

export function statusLabel(status) {
  return LABELS[status] || displayLabel(status) || "Unknown";
}
