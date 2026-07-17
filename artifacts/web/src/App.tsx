import { lazy, Suspense, useEffect, useRef, useState } from "react";
import {
  Switch,
  Route,
  Router as WouterRouter,
  Link,
  useLocation,
} from "wouter";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ThemeProvider, useTheme } from "@/contexts/ThemeContext";
import { LanguageProvider, useLanguage } from "@/contexts/LanguageContext";
import NotFound from "@/pages/not-found";
import {
  LegacyProductionRedirect,
  ProductionEntryRedirect,
} from "@/pages/production-cutover";
import {
  ClipboardCheck,
  Clock,
  Menu,
  X,
  Goal,
  Sun,
  Moon,
} from "lucide-react";

const ProductionPage = lazy(() => import("@/pages/production"));
const HistoryPage = lazy(() =>
  import("@/components/history/GroupedProductionHistory").then((module) => ({
    default: module.GroupedProductionHistory,
  })),
);
const AIAnalysisPage = lazy(() => import("@/pages/ai-analysis"));
const DeliverablePage = lazy(() => import("@/pages/deliverable"));

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 5_000 },
  },
});

function NavItems() {
  const { t } = useLanguage();
  return [
    { path: "/production", label: t.nav.production, icon: ClipboardCheck },
    { path: "/history", label: t.nav.productionHistory, icon: Clock },
  ];
}

function NavLink({
  path,
  label,
  icon: Icon,
  onNavigate,
}: {
  path: string;
  label: string;
  icon: React.ElementType;
  onNavigate?: () => void;
}) {
  const [location] = useLocation();
  const isActive = location.startsWith(path);

  return (
    <Link
      href={path}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors w-full",
        isActive
          ? "bg-primary text-primary-foreground"
          : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
      )}
      data-testid={`nav-link-${label}`}
      aria-current={isActive ? "page" : undefined}
      onClick={onNavigate}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {label}
    </Link>
  );
}

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const { t } = useLanguage();
  const label = theme === "dark" ? t.nav.switchLight : t.nav.switchDark;
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggleTheme}
      className="h-8 w-8"
      data-testid="button-toggle-theme"
      title={label}
      aria-label={label}
    >
      {theme === "dark" ? (
        <Sun className="h-4 w-4" />
      ) : (
        <Moon className="h-4 w-4" />
      )}
    </Button>
  );
}

function LanguageToggle() {
  const { language, setLanguage } = useLanguage();
  return (
    <button
      type="button"
      onClick={() => setLanguage(language === "en" ? "zh" : "en")}
      className="h-8 px-2 rounded-md text-xs font-semibold border border-border hover:bg-sidebar-accent transition-colors text-sidebar-foreground"
      data-testid="button-toggle-language"
      title={language === "en" ? "切换为中文" : "Switch to English"}
    >
      {language === "en" ? "中文" : "EN"}
    </button>
  );
}

function Sidebar({
  onClose,
  onNavigate,
  closeButtonRef,
}: {
  onClose?: () => void;
  onNavigate?: () => void;
  closeButtonRef?: React.Ref<HTMLButtonElement>;
}) {
  const { t } = useLanguage();
  const navItems = NavItems();

  return (
    <div className="flex flex-col h-full">
      {/* Logo */}
      <div className="flex items-center gap-2.5 px-4 py-4 border-b border-sidebar-border">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground shrink-0">
          <Goal className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-bold truncate">{t.nav.appName}</p>
          <p className="text-xs text-muted-foreground">{t.nav.appSub}</p>
        </div>
        {onClose && (
          <Button
            ref={closeButtonRef}
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="ml-auto h-7 w-7 shrink-0"
            data-testid="button-close-sidebar"
            aria-label={t.nav.closeMenu}
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1" aria-label={t.nav.primaryNav}>
        {navItems.map((item) => (
          <NavLink key={item.path} {...item} onNavigate={onNavigate} />
        ))}
      </nav>

      {/* Footer controls */}
      <div className="px-3 py-3 border-t border-sidebar-border space-y-2">
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <LanguageToggle />
        </div>
        <p className="text-xs text-muted-foreground">{t.nav.appFooter}</p>
      </div>
    </div>
  );
}

function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { t } = useLanguage();
  const openButtonRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const mobileDialogRef = useRef<HTMLElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const [location] = useLocation();
  const previousLocationRef = useRef(location);

  function closeMobileAndRestoreFocus() {
    setMobileOpen(false);
    openButtonRef.current?.focus();
  }

  function closeMobileAndFocusMain() {
    setMobileOpen(false);
    mainRef.current?.focus();
  }

  useEffect(() => {
    if (!mobileOpen) return;
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeMobileAndRestoreFocus();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        mobileDialogRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mobileOpen]);

  useEffect(() => {
    if (previousLocationRef.current === location) return;
    previousLocationRef.current = location;
    setMobileOpen(false);
    mainRef.current?.focus();
  }, [location]);

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Desktop sidebar */}
      <aside className="hidden md:flex flex-col w-56 shrink-0 border-r border-sidebar-border bg-sidebar">
        <Sidebar />
      </aside>

      {/* Mobile sidebar overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={closeMobileAndRestoreFocus}
          />
          <aside
            ref={mobileDialogRef}
            role="dialog"
            aria-modal="true"
            className="relative z-10 flex flex-col w-56 h-full bg-sidebar border-r border-sidebar-border"
            aria-label={t.nav.primaryNav}
          >
            <Sidebar
              onClose={closeMobileAndRestoreFocus}
              onNavigate={closeMobileAndFocusMain}
              closeButtonRef={closeButtonRef}
            />
          </aside>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Mobile header */}
        <header className="flex md:hidden items-center gap-3 px-4 py-3 border-b border-border bg-background">
          <Button
            ref={openButtonRef}
            variant="ghost"
            size="icon"
            onClick={() => setMobileOpen(true)}
            data-testid="button-open-sidebar"
            aria-label={t.nav.openMenu}
          >
            <Menu className="h-5 w-5" />
          </Button>
          <div className="flex items-center gap-2 flex-1">
            <Goal className="h-5 w-5 text-primary" />
            <span className="font-bold text-sm">{t.nav.appName}</span>
          </div>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <LanguageToggle />
          </div>
        </header>

        {/* Page content */}
        <main
          ref={mainRef}
          tabIndex={-1}
          className="flex-1 overflow-y-auto p-4 sm:p-6"
        >
          <Suspense
            fallback={
              <p role="status" aria-live="polite">
                {t.common.loading}
              </p>
            }
          >
            <Switch>
              <Route path="/" component={ProductionEntryRedirect} />
              <Route path="/dashboard" component={ProductionEntryRedirect} />
              <Route path="/baseline">
                <LegacyProductionRedirect route="baseline" />
              </Route>
              <Route path="/broadcast">
                <LegacyProductionRedirect route="broadcast" />
              </Route>
              <Route path="/ai" component={AIAnalysisPage} />
              <Route path="/deliverable" component={DeliverablePage} />
              <Route path="/history" component={HistoryPage} />
              <Route path="/production" component={ProductionPage} />
              <Route component={NotFound} />
            </Switch>
          </Suspense>
        </main>
      </div>
    </div>
  );
}

function App() {
  return (
    <ThemeProvider>
      <LanguageProvider>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")}>
              <Layout />
            </WouterRouter>
            <Toaster />
          </TooltipProvider>
        </QueryClientProvider>
      </LanguageProvider>
    </ThemeProvider>
  );
}

export default App;
