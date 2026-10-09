import {
	AppWindow,
	Cable,
	Camera,
	CircleDashed,
	CircleDot,
	Command,
	Hand,
	Keyboard,
	Link,
	Move,
	Pause,
	Pointer,
	Zap,
} from "lucide-react";

import type { Operation } from "@/api/types";
import { Row } from "@/components/list";
import { IconBadge, StatusPill } from "@/components/primitives";
import { date, relativeText, text, timeText } from "@/lib/format";
import {
	type OperationIcon,
	operationLook,
	operationPill,
} from "@/lib/operations";

const ICONS: Record<OperationIcon, typeof Pointer> = {
	pointer: Pointer,
	hand: Hand,
	move: Move,
	keyboard: Keyboard,
	command: Command,
	"circle-dot": CircleDot,
	camera: Camera,
	link: Link,
	"app-window": AppWindow,
	zap: Zap,
	pause: Pause,
	cable: Cable,
	"circle-dashed": CircleDashed,
};

export function OperationRow({
	operation,
	timeStyle,
}: {
	operation: Operation;
	timeStyle: "relative" | "clock";
}) {
	const look = operationLook(operation.kind);
	const pill = operationPill(operation.state);
	const Icon = ICONS[look.icon];
	const stamp = operation.updated ?? operation.created;
	const when =
		timeStyle === "relative"
			? relativeText(stamp)
			: date(stamp)
				? timeText(date(stamp) as Date)
				: "—";
	const subtitle =
		operation.state === "OUTCOME_UNKNOWN"
			? `${when} · check the phone before retrying`
			: when;
	return (
		<Row
			minHeight={56}
			title={`${look.title} · ${text(operation.client, "Unknown client")}`}
			subtitle={subtitle}
			leading={
				<IconBadge
					icon={<Icon />}
					fill={operation.kind === "pause" ? "destructive" : "grey"}
				/>
			}
			trailing={<StatusPill text={pill.text} tone={pill.tone} />}
		/>
	);
}
