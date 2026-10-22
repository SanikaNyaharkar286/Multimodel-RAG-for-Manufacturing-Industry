import { initializeApp } from "firebase/app";
import { getAuth, browserSessionPersistence, setPersistence } from "firebase/auth";

const config = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
};

export const firebaseConfigured = Object.values(config).every(Boolean)
  && !Object.values(config).some(value => String(value).startsWith("your-"));
export const firebaseAuth = firebaseConfigured
  ? getAuth(initializeApp(config))
  : null;

export async function configureBrowserSession() {
  if (firebaseAuth) await setPersistence(firebaseAuth, browserSessionPersistence);
}
