import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Luau Deobfuscator",
  description:
    "Deofuscador de scripts Luau/Lua para uso educativo — sube un script, elige el ofuscador y obtén el código legible.",
  icons: {
    icon:
      "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%94%93%3C/text%3E%3C/svg%3E",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="es">
      <body className="min-h-screen bg-bg antialiased">{children}</body>
    </html>
  );
}
