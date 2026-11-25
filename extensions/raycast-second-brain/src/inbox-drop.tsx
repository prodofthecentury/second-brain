import { Action, ActionPanel, Form, showToast, Toast } from "@raycast/api";
import { callBrainApi } from "./client";

export default function InboxDropCommand() {
  async function handleSubmit(values: { title: string; content: string }) {
    try {
      await callBrainApi("/v1/remember", "POST", { ...values, source: "raycast_inbox" });
      await showToast({ style: Toast.Style.Success, title: "Dropped capture into inbox" });
    } catch (err: any) {
      await showToast({ style: Toast.Style.Failure, title: err?.message || "Failed" });
    }
  }
  return (
    <Form actions={<ActionPanel><Action.SubmitForm title="Drop Capture" onSubmit={handleSubmit} /></ActionPanel>}>
      <Form.TextField id="title" title="Source / Title" />
      <Form.TextArea id="content" title="Text or URL" />
    </Form>
  );
}
