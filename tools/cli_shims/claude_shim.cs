using System;
using System.Diagnostics;
using System.IO;
using System.Text;

class ClaudeShim {
    static int Main(string[] args) {
        string real = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe");
        if (!File.Exists(real)) { Console.Error.WriteLine("claude.exe not found"); return 127; }
        var sb = new StringBuilder();
        foreach (var x in args) { sb.Append(' '); sb.Append(Quote(x)); }
        var psi = new ProcessStartInfo(real, sb.ToString());
        psi.UseShellExecute = false;
        try { using (var p = Process.Start(psi)) { p.WaitForExit(); return p.ExitCode; } }
        catch (Exception e) { Console.Error.WriteLine("shim: " + e.Message); return 126; }
    }
    static string Quote(string s) {
        if (s.Length > 0 && s.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return s;
        return "\"" + s.Replace("\"", "\\\"") + "\"";
    }
}
