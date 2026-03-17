"use client";

import SlantedButton from "./SlantedButton";

export type PendingToolApproval = {
  approvalId: string;
  tool: string;
  summary: string;
};

const ToolApprovalPanel = ({
  approvals,
  onDecision,
}: {
  approvals: PendingToolApproval[];
  onDecision: (approvalId: string, approved: boolean) => void;
}) => {
  if (approvals.length === 0) {
    return null;
  }

  return (
    <div className="mt-4 flex w-full flex-col gap-3">
      {approvals.map((approval) => (
        <div
          key={approval.approvalId}
          className="rounded-2xl border border-white/12 bg-white/6 p-4 backdrop-blur-md"
        >
          <div className="mb-2 text-[10px] uppercase tracking-[0.24em] text-orange">
            Validation requise
          </div>
          <div className="mb-1 text-sm font-semibold text-offwhite">
            {approval.tool.replace("mcp__", "").replace(/__/g, " / ")}
          </div>
          <p className="text-sm leading-6 text-textgray">{approval.summary}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <SlantedButton
              onClick={() => onDecision(approval.approvalId, true)}
              extraClasses="mx-0"
            >
              Valider
            </SlantedButton>
            <SlantedButton
              onClick={() => onDecision(approval.approvalId, false)}
              kind="secondary"
              extraClasses="mx-0"
            >
              Annuler
            </SlantedButton>
          </div>
        </div>
      ))}
    </div>
  );
};

export default ToolApprovalPanel;
