import { EmptyState } from "@/components/EmptyState";

export default function NotFound() {
  return <EmptyState title="No such page" next="Choose a module from the navigation." action={{ label: "Go to the start", href: "/" }} />;
}
