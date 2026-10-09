// Full operation journal, grouped by day, loading older pages as the owner scrolls.

import { createFileRoute } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { useEffect, useRef } from "react";

import { useOperations } from "@/api/queries";
import { Page, PageHeader, Row, Section } from "@/components/list";
import { OperationRow } from "@/components/operation-row";
import { groupByDay } from "@/lib/operations";

export const Route = createFileRoute("/_shell/activity")({
	component: Activity,
});

function Activity() {
	const operations = useOperations(20, 10_000);
	const sentinel = useRef<HTMLDivElement>(null);
	const { hasNextPage, isFetchingNextPage, fetchNextPage } = operations;
	useEffect(() => {
		const node = sentinel.current;
		if (!node || !hasNextPage) return;
		const observer = new IntersectionObserver((entries) => {
			if (entries.some((e) => e.isIntersecting) && !isFetchingNextPage)
				void fetchNextPage();
		});
		observer.observe(node);
		return () => observer.disconnect();
	}, [hasNextPage, isFetchingNextPage, fetchNextPage]);
	const all = operations.data?.pages.flatMap((p) => p.operations) ?? [];
	const groups = groupByDay(all);
	return (
		<Page>
			<PageHeader title="Activity" back="/home" inline />
			{all.length === 0 ? (
				<Section>
					<Row
						minHeight={56}
						title="No activity yet"
						subtitle="AI client actions appear here"
					/>
				</Section>
			) : null}
			{groups.map((group) => (
				<Section key={group.title} title={group.title}>
					{group.items.map((operation) => (
						<OperationRow
							key={operation.id}
							operation={operation}
							timeStyle="clock"
						/>
					))}
				</Section>
			))}
			{hasNextPage ? (
				<div
					ref={sentinel}
					className="flex min-h-11 items-center justify-center text-muted-foreground"
				>
					<Loader2 className="size-5 animate-spin" aria-label="Loading more" />
				</div>
			) : null}
		</Page>
	);
}
