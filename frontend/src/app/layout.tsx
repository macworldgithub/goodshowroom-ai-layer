import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Good Showroom AI",
  description: "Good Showroom AI local workspace",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
