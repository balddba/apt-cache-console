/** Copy text using the Clipboard API or the browser's HTTP-compatible fallback. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // LAN dashboards served over HTTP may not expose the Clipboard API.
  }

  const previousFocus = document.activeElement;
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.readOnly = true;
  textarea.className = 'clipboard-copy';
  document.body.append(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    textarea.remove();
    previousFocus?.focus({ preventScroll: true });
  }
}
