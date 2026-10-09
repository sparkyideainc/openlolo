import { useEffect, useRef, useState } from "react";

/**
 * Whether the box answered recently: three consecutive failed polls mark it unreachable, one
 * success brings it back (ported intent of the app's "Box out of reach" card).
 */
export function useReachable(
	dataUpdatedAt: number,
	errorUpdatedAt: number,
	isError: boolean,
	threshold = 3,
): boolean {
	const failures = useRef(0);
	const [reachable, setReachable] = useState(true);
	// biome-ignore lint/correctness/useExhaustiveDependencies: timestamps are the signal
	useEffect(() => {
		if (isError) {
			failures.current += 1;
			if (failures.current >= threshold) setReachable(false);
		} else {
			failures.current = 0;
			setReachable(true);
		}
	}, [dataUpdatedAt, errorUpdatedAt, isError, threshold]);
	return reachable;
}

/** Live countdown text for a consent request; re-renders once a second. */
export function useTick(active: boolean, ms = 1000): number {
	const [tick, setTick] = useState(0);
	useEffect(() => {
		if (!active) return;
		const id = setInterval(() => setTick((t) => t + 1), ms);
		return () => clearInterval(id);
	}, [active, ms]);
	return tick;
}
