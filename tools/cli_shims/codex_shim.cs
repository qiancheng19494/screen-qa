using System;
using System.Diagnostics;
using System.IO;
using System.Text;

class CodexShim {
    static int Main(string[] args) {
        string node = Environment.GetEnvironmentVariable("MODLENS_NODE");
        if (string.IsNullOrEmpty(node)) node = "node.exe";
        string js = Path.Combine(AppDomain.CurrentDomain.BaseDirectory,
            "..", "..", "..", "..", "..", "Users", "MI", "AppData", "Roaming",
            "npm", "node_modules", "@openai", "codex", "bin", "codex.js");
        js = Path.GetFullPath(js);
        if (!File.Exists(js)) {
            js = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "npm", "node_modules", "@openai", "codex", "bin", "codex.js");
        }
        if (!File.Exists(js)) { Console.Error.WriteLine("codex.js not found"); return 127; }
        return Run(node, Quote(js) + " " + JoinArgs(args), true);
    }
    static string JoinArgs(string[] a) {
        var sb = new StringBuilder();
        foreach (var x in a) { sb.Append(' '); sb.Append(Quote(x)); }
        return sb.ToString();
    }
    static string Quote(string s) {
        if (s.Length > 0 && s.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return s;
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }
    static int Run(string exe, string args, bool passthrough) {
        var psi = new ProcessStartInfo(exe, args);
        psi.UseShellExecute = false;
        psi.RedirectStandardInput = psi.RedirectStandardOutput = psi.RedirectStandardError = false;
        try {
            using (var p = Process.Start(psi)) { p.WaitForExit(); return p.ExitCode; }
        } catch (Exception e) { Console.Error.WriteLine("shim: " + e.Message); return 126; }
    }
}
