package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.ListView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import com.remote.viewer.features.files.FileTransferClient;
import com.remote.viewer.features.notify.Notify;
import com.remote.viewer.features.session.SessionDb;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.Queue;

/**
 * Remote file browser: list, download (pause/resume, queue), upload (queue),
 * mkdir/rename/move/delete. Completed transfers go to the transfer history
 * with a notification.
 */
public class FileManagerActivity extends Activity {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";

    private static final int REQ_PICK = 42;
    private static final int REQ_STORAGE = 43;

    private FileTransferClient ft;
    private ListView listView;
    private TextView pathBar;
    private TextView status;
    private FileAdapter adapter;

    private String currentPath = "";
    private final List<FileTransferClient.Entry> entries = new ArrayList<FileTransferClient.Entry>();

    // active download UI state
    private String dlPath;
    private File dlTmp;
    private int dlPercent = -1;
    private boolean dlPaused;

    // transfer queues (the client runs one download + one upload at a time)
    private final Queue<FileTransferClient.Entry> dlQueue = new ArrayDeque<FileTransferClient.Entry>();
    private final Queue<UploadJob> ulQueue = new ArrayDeque<UploadJob>();
    private boolean ulActive;

    private static class UploadJob {
        File tmp;
        String remote;
    }

    // deferred download while waiting for the storage permission (API 26-28)
    private FileTransferClient.Entry pendingDlEntry;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_files);

        String host = getIntent().getStringExtra(EXTRA_HOST);
        int port = getIntent().getIntExtra(EXTRA_PORT, RemoteProto.PORT);
        String password = getIntent().getStringExtra(EXTRA_PASSWORD);
        if (host == null || password == null) {
            finish();
            return;
        }

        listView = findViewById(R.id.files_list);
        pathBar = findViewById(R.id.files_path);
        status = findViewById(R.id.files_status);
        adapter = new FileAdapter();
        listView.setAdapter(adapter);

        ImageButton back = findViewById(R.id.btn_files_back);
        back.setOnClickListener(v -> finish());

        ImageButton up = findViewById(R.id.btn_files_up);
        up.setOnClickListener(v -> goUp());

        ImageButton mkdir = findViewById(R.id.btn_files_mkdir);
        mkdir.setOnClickListener(v -> showMkdirDialog());

        ImageButton upload = findViewById(R.id.btn_files_upload);
        upload.setOnClickListener(v -> {
            Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            i.addCategory(Intent.CATEGORY_OPENABLE);
            i.setType("*/*");
            startActivityForResult(i, REQ_PICK);
        });

        listView.setOnItemClickListener((parent, view, position, id) -> {
            FileTransferClient.Entry e = entries.get(position);
            if (e.dir) {
                currentPath = join(currentPath, e.name);
                refresh();
            }
        });
        listView.setOnItemLongClickListener((parent, view, position, id) -> {
            showEntryOptions(entries.get(position));
            return true;
        });

        setStatus(getString(R.string.reconnecting));
        ft = new FileTransferClient(this, host, port, password);
        ft.setStatusListener(new FileTransferClient.StatusListener() {
            @Override public void onConnected() { refresh(); }
            @Override public void onAuthFailed(String reason) {
                Toast.makeText(FileManagerActivity.this,
                        R.string.auth_failed, Toast.LENGTH_LONG).show();
                finish();
            }
            @Override public void onDisconnected(boolean willRetry, String reason) {
                if (willRetry) setStatus(getString(R.string.reconnecting));
            }
        });
        ft.start();
    }

    @Override
    protected void onDestroy() {
        if (ft != null) {
            ft.stop();
            ft = null;
        }
        super.onDestroy();
    }

    // ---- navigation ----------------------------------------------------------

    private static String join(String dir, String name) {
        return dir.isEmpty() ? name : dir + "/" + name;
    }

    private void goUp() {
        if (currentPath.isEmpty()) return;
        int i = currentPath.lastIndexOf('/');
        currentPath = i < 0 ? "" : currentPath.substring(0, i);
        refresh();
    }

    private void refresh() {
        updatePathBar();
        setStatus(getString(R.string.reconnecting));
        ft.listDir(currentPath, new FileTransferClient.ListCallback() {
            @Override public void onList(String path, List<FileTransferClient.Entry> list) {
                entries.clear();
                entries.addAll(list);
                Collections.sort(entries, new Comparator<FileTransferClient.Entry>() {
                    @Override public int compare(FileTransferClient.Entry a,
                                                 FileTransferClient.Entry b) {
                        if (a.dir != b.dir) return a.dir ? -1 : 1;
                        return a.name.compareToIgnoreCase(b.name);
                    }
                });
                setStatus("");
                adapter.notifyDataSetChanged();
            }
            @Override public void onError(String reason) {
                setStatus(reason);
            }
        });
    }

    private void updatePathBar() {
        pathBar.setText("~/" + currentPath);
    }

    private void setStatus(final String s) {
        runOnUiThread(new Runnable() {
            @Override public void run() { status.setText(s); }
        });
    }

    // ---- download ------------------------------------------------------------

    private void startDownloadFlow(FileTransferClient.Entry e) {
        if (Build.VERSION.SDK_INT < 29
                && checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED) {
            pendingDlEntry = e;
            requestPermissions(
                    new String[]{android.Manifest.permission.WRITE_EXTERNAL_STORAGE},
                    REQ_STORAGE);
            return;
        }
        queueOrStartDownload(e);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        if (requestCode == REQ_STORAGE) {
            if (grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED
                    && pendingDlEntry != null) {
                startDownload(pendingDlEntry);
            } else {
                Toast.makeText(this, R.string.perm_needed, Toast.LENGTH_LONG).show();
            }
            pendingDlEntry = null;
        }
    }

    private void startDownload(final FileTransferClient.Entry e) {
        final String remote = join(currentPath, e.name);
        dlTmp = new File(getCacheDir(), "dl_" + System.currentTimeMillis());
        dlPath = remote;
        dlPercent = 0;
        dlPaused = false;
        adapter.notifyDataSetChanged();
        ft.download(remote, dlTmp, 0,
                new FileTransferClient.ProgressCallback() {
                    @Override public void onProgress(long done, long total) {
                        int pct = total > 0 ? (int) (done * 100 / total) : 0;
                        if (pct != dlPercent) {
                            dlPercent = pct;
                            adapter.notifyDataSetChanged();
                        }
                    }
                },
                new FileTransferClient.DoneCallback() {
                    @Override public void onDone() {
                        long size = dlTmp != null ? dlTmp.length() : 0;
                        boolean ok = FileTransferClient.installDownload(
                                FileManagerActivity.this, dlTmp, e.name);
                        if (dlTmp != null) dlTmp.delete();
                        dlPath = null;
                        dlPercent = -1;
                        adapter.notifyDataSetChanged();
                        SessionDb.get(FileManagerActivity.this)
                                .logTransfer("down", remote, size,
                                        ok ? "done" : "failed");
                        if (ok) Notify.transferDone(FileManagerActivity.this, e.name);
                        else Notify.transferFailed(FileManagerActivity.this,
                                e.name, "save failed");
                        Toast.makeText(FileManagerActivity.this,
                                ok ? e.name : "save failed",
                                Toast.LENGTH_SHORT).show();
                        pumpDlQueue();
                    }
                    @Override public void onError(String reason) {
                        if (dlTmp != null) dlTmp.delete();
                        dlPath = null;
                        dlPercent = -1;
                        adapter.notifyDataSetChanged();
                        SessionDb.get(FileManagerActivity.this)
                                .logTransfer("down", remote, 0, "failed");
                        Notify.transferFailed(FileManagerActivity.this, e.name, reason);
                        Toast.makeText(FileManagerActivity.this,
                                reason, Toast.LENGTH_LONG).show();
                        pumpDlQueue();
                    }
                });
    }

    /** Queue a download when one is already active; otherwise start it. */
    private void queueOrStartDownload(FileTransferClient.Entry e) {
        if (dlPath != null) {
            dlQueue.add(e);
            updateQueueStatus();
            Toast.makeText(this, getString(R.string.queued, e.name),
                    Toast.LENGTH_SHORT).show();
        } else {
            startDownload(e);
        }
    }

    private void pumpDlQueue() {
        FileTransferClient.Entry next = dlQueue.poll();
        if (next != null) {
            startDownload(next);
        } else {
            updateQueueStatus();
        }
    }

    private void updateQueueStatus() {
        int q = dlQueue.size() + ulQueue.size();
        setStatus(q > 0 ? getString(R.string.queue_status, q) : "");
    }

    private void togglePause() {
        if (dlPath == null) return;
        if (dlPaused) {
            ft.resumeDownload();
            dlPaused = false;
        } else {
            ft.pauseDownload();
            dlPaused = true;
        }
        adapter.notifyDataSetChanged();
    }

    // ---- upload --------------------------------------------------------------

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_PICK && resultCode == RESULT_OK && data != null
                && data.getData() != null) {
            Uri uri = data.getData();
            String name = queryName(uri);
            if (name == null) name = "upload_" + System.currentTimeMillis();
            final File tmp = new File(getCacheDir(), "ul_" + System.currentTimeMillis());
            if (!copyUriToFile(uri, tmp)) {
                Toast.makeText(this, "read failed", Toast.LENGTH_SHORT).show();
                return;
            }
            UploadJob job = new UploadJob();
            job.tmp = tmp;
            job.remote = join(currentPath, name);
            ulQueue.add(job);
            updateQueueStatus();
            pumpUlQueue();
        }
    }

    private void pumpUlQueue() {
        if (ulActive) return;
        final UploadJob job = ulQueue.poll();
        if (job == null) {
            updateQueueStatus();
            return;
        }
        ulActive = true;
        updateQueueStatus();
        final AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle(getString(R.string.uploading))
                .setView(progressView())
                .setCancelable(false)
                .create();
        dlg.show();
        final ProgressBar bar = (ProgressBar) dlg.findViewById(android.R.id.progress);
        ft.upload(job.tmp, job.remote,
                new FileTransferClient.ProgressCallback() {
                    @Override public void onProgress(long done, long total) {
                        if (bar != null && total > 0) {
                            bar.setProgress((int) (done * 100 / total));
                        }
                    }
                },
                new FileTransferClient.DoneCallback() {
                    @Override public void onDone() {
                        long size = job.tmp.length();
                        String name = job.remote.substring(
                                job.remote.lastIndexOf('/') + 1);
                        job.tmp.delete();
                        dlg.dismiss();
                        ulActive = false;
                        SessionDb.get(FileManagerActivity.this)
                                .logTransfer("up", job.remote, size, "done");
                        Notify.transferDone(FileManagerActivity.this, name);
                        refresh();
                        pumpUlQueue();
                    }
                    @Override public void onError(String reason) {
                        String name = job.remote.substring(
                                job.remote.lastIndexOf('/') + 1);
                        job.tmp.delete();
                        dlg.dismiss();
                        ulActive = false;
                        SessionDb.get(FileManagerActivity.this)
                                .logTransfer("up", job.remote, 0, "failed");
                        Notify.transferFailed(FileManagerActivity.this, name, reason);
                        Toast.makeText(FileManagerActivity.this,
                                reason, Toast.LENGTH_LONG).show();
                        pumpUlQueue();
                    }
                });
    }

    private View progressView() {
        ProgressBar bar = new ProgressBar(this, null,
                android.R.attr.progressBarStyleHorizontal);
        bar.setId(android.R.id.progress);
        bar.setMax(100);
        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        bar.setPadding(pad, pad / 2, pad, pad / 2);
        return bar;
    }

    private String queryName(Uri uri) {
        Cursor c = null;
        try {
            c = getContentResolver().query(uri,
                    new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null);
            if (c != null && c.moveToFirst()) return c.getString(0);
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.close();
        }
        return null;
    }

    private boolean copyUriToFile(Uri uri, File dst) {
        InputStream in = null;
        OutputStream os = null;
        try {
            in = getContentResolver().openInputStream(uri);
            os = new FileOutputStream(dst);
            if (in == null) return false;
            byte[] buf = new byte[65536];
            int r;
            while ((r = in.read(buf)) > 0) os.write(buf, 0, r);
            return true;
        } catch (Exception e) {
            return false;
        } finally {
            try { if (in != null) in.close(); } catch (Exception ignored) {}
            try { if (os != null) os.close(); } catch (Exception ignored) {}
        }
    }

    // ---- mkdir / rename / delete ---------------------------------------------

    private void showMkdirDialog() {
        final EditText f = new EditText(this);
        f.setHint(R.string.folder_name);
        f.setSingleLine(true);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        f.setPadding(pad, pad / 2, pad, pad / 2);
        new AlertDialog.Builder(this)
                .setTitle(R.string.new_folder)
                .setView(f)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    String name = f.getText().toString().trim();
                    if (name.isEmpty()) return;
                    ft.mkdir(join(currentPath, name), simpleRefreshCb());
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void showEntryOptions(final FileTransferClient.Entry e) {
        final String old = join(currentPath, e.name);
        new AlertDialog.Builder(this)
                .setTitle(e.name)
                .setItems(new CharSequence[]{getString(R.string.rename),
                                getString(R.string.move),
                                getString(R.string.delete)},
                        (d, which) -> {
                            if (which == 0) showRenameDialog(e);
                            else if (which == 1) showMoveDialog(e, old);
                            else confirmDelete(e);
                        })
                .show();
    }

    /** Move = rename to a different path (same or another directory). */
    private void showMoveDialog(final FileTransferClient.Entry e, final String old) {
        final EditText f = new EditText(this);
        f.setText(old);
        f.setSingleLine(true);
        f.setHint(R.string.new_path);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        f.setPadding(pad, pad / 2, pad, pad / 2);
        new AlertDialog.Builder(this)
                .setTitle(R.string.move)
                .setView(f)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    String dest = f.getText().toString().trim();
                    if (dest.isEmpty() || dest.equals(old)) return;
                    ft.rename(old, dest, simpleRefreshCb());
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void showRenameDialog(final FileTransferClient.Entry e) {
        final EditText f = new EditText(this);
        f.setText(e.name);
        f.setSingleLine(true);
        f.setHint(R.string.new_name);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        f.setPadding(pad, pad / 2, pad, pad / 2);
        new AlertDialog.Builder(this)
                .setTitle(R.string.rename)
                .setView(f)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    String name = f.getText().toString().trim();
                    if (name.isEmpty() || name.equals(e.name)) return;
                    ft.rename(join(currentPath, e.name), join(currentPath, name),
                            simpleRefreshCb());
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void confirmDelete(final FileTransferClient.Entry e) {
        new AlertDialog.Builder(this)
                .setMessage(getString(R.string.delete_file, e.name))
                .setPositiveButton(R.string.delete, (d, w) ->
                        ft.delete(join(currentPath, e.name), simpleRefreshCb()))
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private FileTransferClient.DoneCallback simpleRefreshCb() {
        return new FileTransferClient.DoneCallback() {
            @Override public void onDone() { refresh(); }
            @Override public void onError(String reason) {
                Toast.makeText(FileManagerActivity.this, reason, Toast.LENGTH_LONG).show();
            }
        };
    }

    // ---- list adapter ---------------------------------------------------------

    private class FileAdapter extends BaseAdapter {
        @Override public int getCount() { return entries.size(); }
        @Override public Object getItem(int position) { return entries.get(position); }
        @Override public long getItemId(int position) { return position; }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            final FileTransferClient.Entry e = entries.get(position);
            View v = convertView;
            if (v == null) {
                v = LayoutInflater.from(FileManagerActivity.this)
                        .inflate(R.layout.item_file, parent, false);
            }
            ImageView icon = v.findViewById(R.id.file_icon);
            TextView name = v.findViewById(R.id.file_name);
            TextView size = v.findViewById(R.id.file_size);
            ProgressBar progress = v.findViewById(R.id.file_progress);
            Button action = v.findViewById(R.id.file_action);

            icon.setImageResource(e.dir ? R.drawable.ic_folder : R.drawable.ic_file);
            name.setText(e.name);
            size.setText(e.dir ? "" : humanSize(e.size));

            final String remote = join(currentPath, e.name);
            boolean isDl = remote.equals(dlPath);
            if (e.dir) {
                action.setVisibility(View.GONE);
                progress.setVisibility(View.GONE);
            } else {
                action.setVisibility(View.VISIBLE);
                if (isDl) {
                    action.setText(dlPaused ? R.string.resume : R.string.pause);
                    action.setOnClickListener(vv -> togglePause());
                    progress.setVisibility(View.VISIBLE);
                    progress.setProgress(Math.max(0, dlPercent));
                } else {
                    action.setText(R.string.download);
                    action.setOnClickListener(vv -> startDownloadFlow(e));
                    progress.setVisibility(View.GONE);
                }
            }
            return v;
        }
    }

    private static String humanSize(long bytes) {
        if (bytes < 1024) return bytes + " B";
        double kb = bytes / 1024.0;
        if (kb < 1024) return String.format(Locale.US, "%.1f KB", kb);
        double mb = kb / 1024.0;
        if (mb < 1024) return String.format(Locale.US, "%.1f MB", mb);
        return String.format(Locale.US, "%.2f GB", mb / 1024.0);
    }
}
