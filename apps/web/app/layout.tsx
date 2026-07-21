import type { Metadata } from "next";
import { connection } from "next/server";
import "./styles.css";

export const metadata: Metadata = {
  title: "TubeFactory",
  description: "Evidence-first editorial video production",
  icons: { icon: "/icon.svg" },
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // Nonce-based CSP is request-specific. Force server rendering so Next can
  // read the proxy-generated nonce and attach it to every bootstrap script.
  await connection();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
