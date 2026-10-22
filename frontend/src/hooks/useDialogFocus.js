import { useEffect, useRef } from 'react';

// Keep keyboard navigation inside a modal and restore the invoking control.
export default function useDialogFocus(open, onClose) {
  const dialog = useRef(null);
  const close = useRef(onClose);
  useEffect(() => { close.current = onClose; }, [onClose]);
  useEffect(() => {
    if (!open || !dialog.current) return undefined;
    const previous = document.activeElement;
    const root = dialog.current;
    const controls = () => [...root.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, [tabindex="0"]')]
      .filter(element => element.getClientRects().length);
    (controls()[0] || root).focus();
    const handle = event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close.current(); }
      if (event.key !== 'Tab') return;
      const items = controls();
      if (!items.length) { event.preventDefault(); root.focus(); return; }
      if (event.shiftKey && (document.activeElement === items[0] || !root.contains(document.activeElement))) {
        event.preventDefault(); items.at(-1).focus();
      } else if (!event.shiftKey && (document.activeElement === items.at(-1) || !root.contains(document.activeElement))) {
        event.preventDefault(); items[0].focus();
      }
    };
    root.addEventListener('keydown', handle);
    return () => { root.removeEventListener('keydown', handle); if (previous?.isConnected) previous.focus(); };
  }, [open]);
  return dialog;
}
