import { createContext, useContext } from "react";

const AuthProfileContext = createContext(null);

export const AuthProfileProvider = AuthProfileContext.Provider;

export function useAuthProfile() {
  return useContext(AuthProfileContext);
}
