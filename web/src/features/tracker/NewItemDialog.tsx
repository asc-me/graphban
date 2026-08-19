import { Plus } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { useProjectCtx } from "@/features/ProjectContext";
import { useCreateItem } from "@/lib/queries";

export function NewItemDialog() {
  const { activeId } = useProjectCtx();
  const create = useCreateItem(activeId);
  const [open, setOpen] = React.useState(false);
  const [title, setTitle] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [tags, setTags] = React.useState("");
  const [effort, setEffort] = React.useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    await create.mutateAsync({
      title: title.trim(),
      description: description.trim(),
      tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      effort: Number(effort) || 0,
    });
    setTitle("");
    setDescription("");
    setTags("");
    setEffort("");
    setOpen(false);
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm">
          <Plus size={14} />
          New item
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New tracker item</DialogTitle>
          <DialogDescription>Drops into the backlog at the top of the stream.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <Input placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
          <Textarea
            placeholder="Description (markdown)"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
          />
          <div className="flex gap-3">
            <Input placeholder="tags, comma, sep" value={tags} onChange={(e) => setTags(e.target.value)} />
            <Input
              placeholder="effort"
              type="number"
              value={effort}
              onChange={(e) => setEffort(e.target.value)}
              className="w-24"
            />
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button type="button" variant="outline" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" size="sm" disabled={!title.trim() || create.isPending}>
              Create item
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
