# Run only after Firebase Admin SDK and ADC are configured.
# This template deliberately requires an explicit existing Firebase UID/email.
param(
  [Parameter(Mandatory=$true)][string]$AdminEmail,
  [string]$AdminUid = ""
)

Write-Host "Bootstrap requires the privileged Python command from this folder."
Write-Host "Email: $AdminEmail"
Write-Host "UID: $AdminUid"
Write-Host "No browser or public endpoint may be used for admin bootstrap."
