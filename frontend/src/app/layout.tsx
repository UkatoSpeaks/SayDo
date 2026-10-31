import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--font-geist", display: "swap" });
const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-geist-mono",
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000"),
  title: { default: "SayDo", template: "%s · SayDo" },
  description: "Track what management promised against what they delivered.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // The home page's inline script sets data-motion on <html> before hydration.
    <html lang="en-IN" className={`${geist.variable} ${geistMono.variable}`} suppressHydrationWarning>
      <body className="min-h-screen bg-paper text-ink">{children}</body>
    </html>
  );
}
