"use client";

/**
 * Sticky marketing navigation with active-page highlighting (usePathname).
 * Shared across the home and the /features, /how-it-works, /why-different pages
 * via the (marketing) route-group layout.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ArrowRight } from "lucide-react";
import {
  APP_HREF,
  CtaLink,
  GITHUB_HREF,
  GithubMark,
  Logo,
  Wordmark,
} from "@/components/marketing";

const LINKS = [
  { href: "/features", label: "Features" },
  { href: "/how-it-works", label: "How it works" },
  { href: "/why-different", label: "Why different" },
];

export function SiteNav() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-50 border-b-2 border-ink bg-paper2/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center gap-4 px-5 py-3">
        <Link href="/" className="flex items-center gap-3" aria-label="Pipeline Copilot home">
          <Logo />
          <Wordmark />
        </Link>

        <nav className="ml-auto hidden items-center gap-7 text-sm font-medium md:flex">
          {LINKS.map((l) => {
            const active = pathname === l.href;
            return (
              <Link
                key={l.href}
                href={l.href}
                aria-current={active ? "page" : undefined}
                className={`relative transition-colors after:absolute after:-bottom-1.5 after:left-0 after:h-0.5 after:rounded-full after:bg-terra after:transition-all ${
                  active
                    ? "text-ink after:w-full"
                    : "text-inksoft after:w-0 hover:text-ink hover:after:w-full"
                }`}
              >
                {l.label}
              </Link>
            );
          })}
          <a
            href={GITHUB_HREF}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 text-inksoft transition-colors hover:text-ink"
          >
            <GithubMark className="h-4 w-4" /> GitHub
          </a>
        </nav>

        <CtaLink href={APP_HREF} size="md" className="ml-auto md:ml-0">
          Analyze Pipeline
          <ArrowRight className="h-4 w-4" />
        </CtaLink>
      </div>
    </header>
  );
}

export default SiteNav;
