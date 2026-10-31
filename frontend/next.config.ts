import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Lets a second build (e.g. NEXT_DIST_DIR=.next/verify) sit beside a running `next start`.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
