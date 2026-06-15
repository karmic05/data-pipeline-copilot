/**
 * Layout for the marketing route group: the home page plus /features,
 * /how-it-works and /why-different all share the sticky nav, the closing CTA,
 * and the footer. The analyzer at /app sits outside this group and keeps its
 * own chrome.
 */
import type { ReactNode } from "react";
import { SiteNav } from "@/components/site-nav";
import { FinalCta, Footer } from "@/components/marketing";

export default function MarketingLayout({ children }: { children: ReactNode }) {
  return (
    <div className="bg-paper text-ink">
      <SiteNav />
      <main>{children}</main>
      <FinalCta />
      <Footer />
    </div>
  );
}
