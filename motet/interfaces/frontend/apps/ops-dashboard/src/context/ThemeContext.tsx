/**
 * Theme context so pages (e.g. DeveloperDocsPage) can read dark mode for Mermaid/docs rendering.
 *
 * Last Modified: 2026-08-24
 */
import { createContext, useContext, useMemo } from "react";

interface ThemeContextValue {
  darkMode: boolean;
}

const ThemeContext = createContext<ThemeContextValue>({ darkMode: true });

export function ThemeProvider({
  darkMode,
  children,
}: {
  darkMode: boolean;
  children: React.ReactNode;
}) {
  const value = useMemo(() => ({ darkMode }), [darkMode]);
  return (
    <ThemeContext.Provider value={value}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): boolean {
  return useContext(ThemeContext).darkMode;
}
