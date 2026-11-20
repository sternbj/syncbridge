import SwiftUI

/// Pending deletions grouped by folder: restore them or delete the remaining copies.
struct DeletionsView: View {
    @EnvironmentObject var model: AppModel
    @State private var selected: Set<DeletionGroup.ID> = []
    @State private var confirmDelete = false
    @State private var result: String?

    private var selectedItems: [DeletionItem] {
        model.deletionGroups.filter { selected.contains($0.id) }.flatMap(\.items)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("\(model.deletionGroups.reduce(0) { $0 + $1.count }) file(s) were deleted on one side and wait for your decision.")
                    .font(.headline)
                Spacer()
                Button(selected.count == model.deletionGroups.count ? "Select None" : "Select All") {
                    selected = selected.count == model.deletionGroups.count ? [] : Set(model.deletionGroups.map(\.id))
                }
                .disabled(model.deletionGroups.isEmpty)
            }
            .padding(10)
            Divider()
            if model.deletionGroups.isEmpty {
                Text("No pending deletions 🎉")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(model.deletionGroups) { group in
                    row(group)
                }
            }
            Divider()
            HStack {
                Text("Restore copies the remaining file back. Delete removes it (local files go to the sync trash, OneDrive files to the OneDrive recycle bin).")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                if model.isWorking { ProgressView().controlSize(.small) }
                Button("Restore \(selectedItems.count)") { run(restore: true) }
                    .disabled(selectedItems.isEmpty || model.isWorking)
                Button("Delete \(selectedItems.count)…", role: .destructive) { confirmDelete = true }
                    .disabled(selectedItems.isEmpty || model.isWorking)
            }
            .padding(10)
        }
        .task { await model.loadDeletions() }
        .confirmationDialog(
            "Delete \(selectedItems.count) file(s)?",
            isPresented: $confirmDelete
        ) {
            Button("Delete", role: .destructive) { run(restore: false) }
        } message: {
            Text("Only the selected files are deleted. Each file is checked against its last synced version first; if anything changed, nothing is deleted.")
        }
        .alert("Pending Deletions", isPresented: Binding(get: { result != nil }, set: { if !$0 { result = nil } })) {
            Button("OK") { result = nil }
        } message: {
            Text(result ?? "")
        }
    }

    private func row(_ group: DeletionGroup) -> some View {
        HStack(alignment: .top) {
            Toggle("", isOn: Binding(
                get: { selected.contains(group.id) },
                set: { isOn in if isOn { selected.insert(group.id) } else { selected.remove(group.id) } }
            ))
            .labelsHidden()
            DisclosureGroup {
                ForEach(group.items.prefix(200), id: \.self) { item in
                    Text(item.file).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                }
                if group.count > 200 {
                    Text("… and \(group.count - 200) more").font(.caption).foregroundStyle(.secondary)
                }
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(group.label).lineLimit(1).truncationMode(.head)
                        if group.recommended {
                            Text("RECOMMENDED").font(.caption2.bold()).foregroundStyle(.green)
                        }
                    }
                    Text("\(group.count) file(s) · still on \(group.remainingOn) · \(group.pair)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func run(restore: Bool) {
        let items = selectedItems
        Task {
            result = await model.applyDeletions(items, restore: restore)
            // Keep the selection of groups that are still open (e.g. after an error).
            selected = selected.filter { id in model.deletionGroups.contains { $0.id == id } }
        }
    }
}
