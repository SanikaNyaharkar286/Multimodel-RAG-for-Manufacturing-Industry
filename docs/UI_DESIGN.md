# Workspace UI

The selected background is option 2, soft 3D machinery. The asset is saved in
`frontend/src/assets/workspace-machinery-v1.png`; the original `hero.png` remains available.
It was created with the built-in image-generation tool, then selected by the user.

Generation prompt:

> Use case: stylized-concept. Create a polished 16:9 landscape website BACKGROUND ONLY for a Manufacturing Equipment Maintenance Copilot. Option 2: elegant minimal industrial illustration with a warm white and pale blue-gray base. Subtle softly shaded isometric 3D mechanical elements: a precision gear, bearing and compact robot gripper, arranged sparsely at the far corners. Tiny touches of desaturated teal and blue, clean premium enterprise aesthetic, diffuse daylight, mostly near-white, not dark. Central 65 percent quiet and almost empty for chat cards. No text, labels, numerals, logos, interface, buttons, cards, people or watermark. Decorative wallpaper, not a technical instruction.

The welcome page uses compact spacing for short screens, selectable question cards,
and the heading “Let’s troubleshoot.” Main actions are blue, resolution is green,
and reporting an observation uses a blue outline. Technical service failures show a
plain retry notice. The case status strip is removed.

The administration page is available through the admin profile menu at `#admin`.
It replaces the overlay, provides pending/all filters and search, and retains the
chat component while navigating back and forth. Existing approval authorization
and APIs remain in place. Email approval links continue to select the request.

Verified with the frontend test suite, lint, and production build. Isolated browser
previews used synthetic records: the welcome page had no horizontal or vertical
overflow at 1280×620 and 390×844; admin layouts and filters were checked at desktop
and phone sizes. Report modal keyboard focus and Escape, prompt selection, and
the repair observation/upload form were also checked. Live account approvals were
not changed during UI verification.
