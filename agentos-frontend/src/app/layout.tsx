import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/contexts/AuthContext";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
});

export const metadata: Metadata = {
  title: "1mg AgentOS — Autonomous Business Operations",
  description: "Agent Operating System for Tata 1mg. Chat-first, inline approvals, progressive autonomy.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${inter.variable} h-full antialiased`}>
      <body className={`${inter.variable} font-sans min-h-full flex flex-col bg-[var(--bg-primary)] text-[var(--text-primary)]`}>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
