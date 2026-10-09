// The three-tab shell: floating pill tab bar on phones (the app canvas), a left rail from
// 768 px up.

import { cn } from "@openlolo/ui/lib/utils";
import { Link } from "@tanstack/react-router";
import { House, Settings, Users } from "lucide-react";
import type { ReactNode } from "react";

const TABS = [
	{ to: "/home", title: "Home", icon: House },
	{ to: "/access", title: "Access", icon: Users },
	{ to: "/settings", title: "Settings", icon: Settings },
] as const;

export function Shell({
	children,
	badge = 0,
}: {
	children: ReactNode;
	badge?: number;
}) {
	return (
		<div className="min-h-dvh bg-background md:flex">
			{/* Floating pill (iOS 26 tab bar, the app canvas): lifted off the bottom edge so the
			    standalone viewport quirk (src/lib/viewport-gap.ts) can never show under it. */}
			<nav
				aria-label="Sections"
				className="fixed inset-x-4 bottom-[calc(env(safe-area-inset-bottom)+0.75rem-var(--viewport-gap,0px))] z-40 mx-auto max-w-[448px] rounded-[28px] border border-border/70 bg-card/90 shadow-[0_8px_30px_-8px_rgba(43,37,33,0.35)] backdrop-blur-xl md:static md:mx-0 md:w-56 md:max-w-none md:shrink-0 md:rounded-none md:border-0 md:border-border md:border-r md:bg-transparent md:pt-6 md:shadow-none md:backdrop-blur-none"
			>
				<ul className="flex justify-around px-2 md:flex-col md:gap-1 md:px-3">
					{TABS.map(({ to, title, icon: Icon }) => (
						<li key={to} className="flex-1 md:flex-none">
							<Link
								to={to}
								className="flex h-14 flex-col items-center justify-center gap-[3px] rounded-[20px] font-medium text-[10px] text-muted-foreground leading-3 transition-colors md:h-auto md:flex-row md:gap-3 md:rounded-xl md:px-3 md:py-2.5 md:text-[15px] md:leading-normal md:hover:bg-card"
								activeProps={{ className: "text-primary md:bg-card" }}
								activeOptions={{ exact: false }}
							>
								<span className="relative">
									<Icon className="size-[24px] md:size-5" strokeWidth={1.8} />
									{to === "/access" && badge > 0 ? (
										<span
											className="absolute -top-1 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 font-bold text-[10px] text-white"
											role="status"
											aria-label={`${badge} waiting`}
										>
											{badge}
										</span>
									) : null}
								</span>
								{title}
							</Link>
						</li>
					))}
				</ul>
			</nav>
			<main
				className={cn(
					"mx-auto w-full max-w-[480px] px-4 pt-[calc(env(safe-area-inset-top)+1rem)] pb-[calc(env(safe-area-inset-bottom)+6.5rem)] md:pt-6 md:pb-10",
				)}
			>
				{children}
			</main>
		</div>
	);
}

/** Single-column container for screens outside the tabs (onboarding, connect). */
export function Plain({
	children,
	className,
}: {
	children: ReactNode;
	className?: string;
}) {
	return (
		<div className="min-h-dvh bg-background">
			<main
				className={cn(
					"mx-auto flex min-h-dvh w-full max-w-[480px] flex-col px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))]",
					className,
				)}
			>
				{children}
			</main>
		</div>
	);
}
