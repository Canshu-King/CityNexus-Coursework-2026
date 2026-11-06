import { useEffect, useState } from "react";
import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  arrayMove,
  sortableKeyboardCoordinates,
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useT } from "../../i18n/LocaleContext";

function SortableActivity({ activity, label, index, protectedActivity }: { activity: string; label: string; index: number; protectedActivity: boolean }) {
  const sortable = useSortable({ id: activity });
  const t = useT();
  const style = { transform: CSS.Transform.toString(sortable.transform), transition: sortable.transition };
  return (
    <div ref={sortable.setNodeRef} style={style} className={protectedActivity ? "v8-activity protected" : "v8-activity"} {...sortable.attributes} {...sortable.listeners}>
      <i>{index === 0 ? "•" : "○"}</i><span>{label}</span>{protectedActivity && <em>{t("studio.protectedLabel")}</em>}
    </div>
  );
}

export function SortableActivityList({ activities, protectedFirst, onDraft, labels }: { activities: string[]; protectedFirst: boolean; onDraft?: (activities: string[]) => void; labels?: Record<string, string> }) {
  const [items, setItems] = useState<string[]>(activities);
  /* dnd-kit's own `aria-describedby` on every row already tells a keyboard or screen-reader user to
     "press the space bar … use the arrow keys". With only a PointerSensor registered that promise
     was false: the row focused and nothing moved (ORD-0369, measured on a 390x844 viewport). */
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  useEffect(() => {
    setItems(activities);
  }, [activities]);
  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const oldIndex = items.indexOf(String(active.id));
    const newIndex = items.indexOf(String(over.id));
    const next = arrayMove<string>(items, oldIndex, newIndex);
    setItems(next);
    onDraft?.(next);
  };
  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
      <SortableContext items={items} strategy={verticalListSortingStrategy}>
        {items.map((activity, index) => <SortableActivity key={activity} activity={activity} label={labels?.[activity] ?? activity} index={index} protectedActivity={protectedFirst && index === 0} />)}
      </SortableContext>
    </DndContext>
  );
}
