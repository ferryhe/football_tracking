import { useState } from "react";
import { Switch, Route, Router as WouterRouter, Link, useLocation } from "wouter";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ThemeProvider, useTheme } from "@/contexts/ThemeContext";
import { LanguageProvider, useLanguage } from "@/contexts/LanguageContext";
import NotFound from "@/pages/not-found";
import DashboardPage from "@/pages/dashboard";
import BaselinePage from "@/pages/baseline";
import AIAnalysisPage from "@/pages/ai-analysis";
import DeliverablePage from "@/pages/deliverable";
import HistoryPage from "@/pages/history";
import {
  LayoutDashboard,
  Play,
  Sparkles,
  Film,
  Clock,
  Menu,
  X,
  Goal,
  Sun,
  Moon,
} from "lucide-react";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 5_000 },
  },
});

function NavItems() {
  const { t } = useLanguage();
  return [
    { path: "/", label: t.nav.dashboard, icon: LayoutDashboard },
    { path: "/baseline", label: t.nav.baseline, icon: Play },
    { path: "/ai", label: t.nav.aiAnalysis, icon: Sparkles },
    { path: "/deliverable", label: t.nav.deliverable, icon: Film },
    { path: "/history", label: t.nav.history, icon: Clock },
  ];
}

function NavLink({ path, label, icon: Icon }: { path: string; label: string; icon: React.ElementType }) {
  const [location] = useLocation();
  const isActive = path === "/" ? location === "/" : location.startsWith(path);

  return (
    <Link
      href={path}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors w-full",
        isActive
          ? "bg-primary text-primary-foreground"
          : "text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
      )}
      data-testid={`nav-link-${label}`}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {label}
    </Link>
  );
}

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggleTheme}
      className="h-8 w-8"
      data-testid="button-toggle-theme"
      title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
    >
      {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
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

function Sidebar({ onClose }: { onClose?: () => void }) {
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
            variant="ghost"
            size="icon"
            onClick={onClose}
            className="ml-auto h-7 w-7 shrink-0"
            data-testid="button-close-sidebar"
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {navItems.map((item) => (
          <NavLink key={item.path} {...item} />
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
            onClick={() => setMobileOpen(false)}
          />
          <aside className="relative z-10 flex flex-col w-56 h-full bg-sidebar border-r border-sidebar-border">
            <Sidebar onClose={() => setMobileOpen(false)} />
          </aside>
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Mobile header */}
        <header className="flex md:hidden items-center gap-3 px-4 py-3 border-b border-border bg-background">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setMobileOpen(true)}
            data-testid="button-open-sidebar"
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
        <main className="flex-1 overflow-y-auto p-4 sm:p-6">
          <Switch>
            <Route path="/" component={DashboardPage} />
            <Route path="/baseline" component={BaselinePage} />
            <Route path="/ai" component={AIAnalysisPage} />
            <Route path="/deliverable" component={DeliverablePage} />
            <Route path="/history" component={HistoryPage} />
            <Route component={NotFound} />
          </Switch>
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
