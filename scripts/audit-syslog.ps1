# Count lines per program per day in one big syslog file (the "top talkers" audit). Read-only.
#
# Streams the file once (a 4 GB file takes about a minute), opening it with shared read access.
# It never writes to the share. Classic BSD syslog lines are expected ("Mon dd hh:mm:ss host program: ...").
#   .\scripts\audit-syslog.ps1 -Path \\your-server\your-share\syslog\syslog-<ip>.log
#   .\scripts\audit-syslog.ps1 -Path <file> -Out <your instance>\private\top-talkers.txt
# The result names your hosts and programs, so keep it in your instance folder, not in this repository.
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

public static class TopTalkers
{
    public static string Run(string path)
    {
        var perDayProg = new Dictionary<string, long>();
        var perDay = new Dictionary<string, long>();
        var perProg = new Dictionary<string, long>();
        long total = 0, unparsed = 0;
        var sw = System.Diagnostics.Stopwatch.StartNew();
        using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite, 1 << 20, FileOptions.SequentialScan))
        using (var sr = new StreamReader(fs, Encoding.UTF8, false, 1 << 20))
        {
            string line;
            while ((line = sr.ReadLine()) != null)
            {
                total++;
                if (line.Length < 20 || line[3] != ' ' || line[6] != ' ' || line[9] != ':') { unparsed++; continue; }
                string day = line.Substring(0, 6);
                int hs = line.IndexOf(' ', 16);
                if (hs < 0) { unparsed++; continue; }
                int ps = hs + 1;
                int pe = ps;
                while (pe < line.Length && line[pe] != ' ' && line[pe] != ':' && line[pe] != '[') pe++;
                string prog = pe > ps ? line.Substring(ps, pe - ps) : "(none)";
                long c;
                string k = day + "|" + prog;
                perDayProg.TryGetValue(k, out c); perDayProg[k] = c + 1;
                perDay.TryGetValue(day, out c); perDay[day] = c + 1;
                perProg.TryGetValue(prog, out c); perProg[prog] = c + 1;
            }
        }
        var sb = new StringBuilder();
        sb.AppendLine("total_lines=" + total + " unparsed=" + unparsed + " distinct_programs=" + perProg.Count + " seconds=" + sw.Elapsed.TotalSeconds.ToString("0"));
        sb.AppendLine("== lines per day ==");
        var days = new List<string>(perDay.Keys);
        days.Sort(StringComparer.Ordinal);
        foreach (var d in days) sb.AppendLine(d + "\t" + perDay[d]);
        sb.AppendLine("== top programs overall ==");
        var progs = new List<KeyValuePair<string, long>>(perProg);
        progs.Sort((a, b) => b.Value.CompareTo(a.Value));
        for (int i = 0; i < Math.Min(30, progs.Count); i++)
            sb.AppendLine(progs[i].Key + "\t" + progs[i].Value + "\t" + (100.0 * progs[i].Value / Math.Max(1, total)).ToString("0.00") + "%");
        sb.AppendLine("== top 3 programs on each of the 5 busiest days ==");
        var busy = new List<KeyValuePair<string, long>>(perDay);
        busy.Sort((a, b) => b.Value.CompareTo(a.Value));
        for (int i = 0; i < Math.Min(5, busy.Count); i++)
        {
            string d = busy[i].Key;
            var rows = new List<KeyValuePair<string, long>>();
            foreach (var kv in perDayProg) if (kv.Key.StartsWith(d + "|", StringComparison.Ordinal)) rows.Add(kv);
            rows.Sort((a, b) => b.Value.CompareTo(a.Value));
            sb.Append(d + " (" + busy[i].Value + "):");
            for (int j = 0; j < Math.Min(3, rows.Count); j++) sb.Append("  " + rows[j].Key.Substring(7) + "=" + rows[j].Value);
            sb.AppendLine();
        }
        return sb.ToString();
    }
}
'@
$result = [TopTalkers]::Run($Path)
if ($Out) { [System.IO.File]::WriteAllText($Out, $result); Write-Host "Wrote $Out" } else { $result }
