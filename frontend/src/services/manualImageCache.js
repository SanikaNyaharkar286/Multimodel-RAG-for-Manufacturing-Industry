import { firebaseAuth } from '../auth/firebase';

// Memory only, scoped to the signed-in user and cleared with the chat. A
// shared promise also prevents duplicate downloads during React remounts.
const images = new Map();

function key(src) {
  return `${firebaseAuth?.currentUser?.uid || 'local'}:${src}`;
}

export function cachedManualImage(src) {
  return images.get(key(src))?.url || null;
}

export function loadManualImage(src, force = false) {
  const identity = key(src);
  const existing = images.get(identity);
  if (existing && !force) return existing.promise;
  if (existing) {
    existing.controller.abort();
    if (existing.url) URL.revokeObjectURL(existing.url);
  }
  const entry = {url: null, controller: new AbortController()};
  images.set(identity, entry);
  entry.promise = (async () => {
    try {
      const user = firebaseAuth?.currentUser;
      let token = user ? await user.getIdToken() : null;
      const request = () => fetch(src, {signal: entry.controller.signal,
        headers: token ? {Authorization: `Bearer ${token}`} : {}});
      let response = await request();
      if (response.status === 401 && user) {
        token = await user.getIdToken(true);
        response = await request();
      }
      if (!response.ok) throw new Error(`image-${response.status}`);
      const blob = await response.blob();
      if (images.get(identity) !== entry || entry.controller.signal.aborted) {
        throw new Error('Image cache cleared');
      }
      entry.url = URL.createObjectURL(blob);
      return entry.url;
    } catch (error) {
      if (images.get(identity) === entry) images.delete(identity);
      throw error;
    }
  })();
  return entry.promise;
}

export function preloadStepImages(previews) {
  for (const preview of previews || []) {
    for (const block of preview.response?.blocks || []) {
      if (block.type === 'image' && block.src) loadManualImage(block.src).catch(() => {});
    }
  }
}

export function clearManualImageCache() {
  for (const entry of images.values()) {
    entry.controller.abort();
    if (entry.url) URL.revokeObjectURL(entry.url);
  }
  images.clear();
}
