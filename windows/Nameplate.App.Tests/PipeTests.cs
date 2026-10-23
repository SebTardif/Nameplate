using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Nameplate.App;
using Xunit;

namespace Nameplate.App.Tests;

public sealed class PipeTests
{
    [Fact]
    public async Task CommandsReachTheCurrentSessionServer()
    {
        var received = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        using var server = new PipeServer(json => received.TrySetResult(json));
        const string command = "{\"command\":\"splash\"}";

        Assert.Equal(0, Program.Main(["splash"]));
        Assert.Equal(command, await received.Task.WaitAsync(TimeSpan.FromSeconds(5)));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task OlderTrayGetsActionableRecoveryWithoutUsingItsGlobalPipe(bool startIfNeeded)
    {
        using var legacy = new NamedPipeServerStream("nameplate", PipeDirection.In, 1,
            PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
        using var mutex = new Mutex(false, Program.MutexName);
        using var error = new StringWriter();
        var originalError = Console.Error;
        Console.SetError(error);
        try
        {
            Assert.Equal(1, await PipeClient.SendAsync("{\"command\":\"splash\"}", startIfNeeded));
            Assert.Contains("Quit Nameplate", error.ToString());
            Assert.Contains("notification area", error.ToString());
            Assert.False(legacy.IsConnected);
        }
        finally
        {
            Console.SetError(originalError);
        }
    }

    [Fact]
    public async Task PipeAccessBelongsToTheUserRatherThanTheElevatedOwnerGroup()
    {
        using var server = new PipeServer(_ => { });
        using var client = new NamedPipeClientStream(".", PipeServer.PipeName, PipeDirection.Out);
        await client.ConnectAsync(5000);
        using var identity = WindowsIdentity.GetCurrent();
        var rules = client.GetAccessControl().GetAccessRules(true, true, typeof(SecurityIdentifier))
            .Cast<PipeAccessRule>().Where(rule => rule.AccessControlType == AccessControlType.Allow).ToArray();

        Assert.NotEmpty(rules);
        Assert.All(rules, rule => Assert.Equal(identity.User, rule.IdentityReference));
    }

    [AdministratorFact]
    public async Task AnotherAccountIsRejectedBeforeCommandDispatch()
    {
        var dispatched = 0;
        using var server = new PipeServer(_ => Interlocked.Increment(ref dispatched));
        var outcome = await RunScheduledProbe($$"""
            $pipe = [System.IO.Pipes.NamedPipeClientStream]::new('.', '{{PipeServer.PipeName}}', [System.IO.Pipes.PipeDirection]::Out)
            try {
                $pipe.Connect(3000)
                $writer = [System.IO.StreamWriter]::new($pipe)
                $writer.WriteLine('{"command":"splash"}')
                $writer.Flush()
                $outcome = 'CONNECTED'
            } catch {
                $outcome = if ($_.Exception.GetBaseException() -is [UnauthorizedAccessException]) { 'DENIED' } else { 'UNEXPECTED_FAILURE' }
            } finally {
                $pipe.Dispose()
            }
            """, sameUser: false);

        Assert.Equal("DENIED", outcome);
        Assert.Equal(0, Volatile.Read(ref dispatched));
    }

    [InteractiveAdministratorFact]
    public async Task CliInAnotherSessionDeliversOnlyToThatSessionsPipe()
    {
        var dispatched = 0;
        using var server = new PipeServer(_ => Interlocked.Increment(ref dispatched));
        var dotnet = Path.GetFullPath(Path.Combine(RuntimeEnvironment.GetRuntimeDirectory(), "..", "..", "..", "dotnet.exe"));
        var runtime = Path.Combine(AppContext.BaseDirectory, "Nameplate.App.Tests.runtimeconfig.json");
        var deps = Path.Combine(AppContext.BaseDirectory, "Nameplate.App.Tests.deps.json");
        var assembly = typeof(Program).Assembly.Location;
        var arguments = $"exec --runtimeconfig \"{runtime}\" --depsfile \"{deps}\" \"{assembly}\" splash";
        var outcome = await RunScheduledProbe($$"""
            $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
            $pipe = [System.IO.Pipes.NamedPipeServerStream]::new("nameplate-$session", [System.IO.Pipes.PipeDirection]::In, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::Asynchronous)
            $client = $null
            try {
                $connection = $pipe.WaitForConnectionAsync()
                $client = Start-Process -FilePath '{{dotnet.Replace("'", "''")}}' -ArgumentList '{{arguments.Replace("'", "''")}}' -PassThru
                if (-not $connection.Wait(10000)) { throw 'CLI did not connect to its own session' }
                $reader = [System.IO.StreamReader]::new($pipe)
                $line = $reader.ReadLineAsync()
                if (-not $line.Wait(10000)) { throw 'CLI did not send a command' }
                if (-not $client.WaitForExit(10000) -or $client.ExitCode -ne 0) { throw 'CLI failed' }
                if (($line.Result | ConvertFrom-Json).command -ne 'splash') { throw 'Incorrect command' }
                $outcome = "RECEIVED:$session"
            } finally {
                if ($client -and -not $client.HasExited) { Stop-Process -Id $client.Id -Force }
                $pipe.Dispose()
            }
            """, sameUser: true);

        Assert.StartsWith("RECEIVED:", outcome);
        Assert.NotEqual(Process.GetCurrentProcess().SessionId, int.Parse(outcome.Split(':')[1]));
        Assert.Equal(0, Volatile.Read(ref dispatched));
    }

    private static async Task<string> RunScheduledProbe(string body, bool sameUser)
    {
        var directory = Path.Combine(Path.GetTempPath(), "nameplate-pipe-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var task = "NameplatePipeTest-" + Guid.NewGuid().ToString("N");
        var script = Path.Combine(directory, "probe.ps1");
        var result = Path.Combine(directory, "result.txt");
        File.WriteAllText(script, $$"""
            $ErrorActionPreference = 'Stop'
            try {
                {{body}}
            } catch {
                $outcome = 'FAILED:' + $_.Exception.GetBaseException().GetType().Name
            }
            [System.IO.File]::WriteAllText('{{result.Replace("'", "''")}}.tmp', $outcome)
            [System.IO.File]::Move('{{result.Replace("'", "''")}}.tmp', '{{result.Replace("'", "''")}}')
            """);
        try
        {
            using var identity = WindowsIdentity.GetCurrent();
            var arguments = new List<string> { "/Create", "/TN", task, "/SC", "ONCE", "/ST", "00:00", "/RU",
                sameUser ? identity.Name : "SYSTEM", "/TR",
                $"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{script}\"", "/F" };
            if (sameUser)
            {
                // S4U runs the same user noninteractively in session zero, without storing credentials.
                arguments.Add("/NP");
            }
            await RunTaskScheduler(arguments.ToArray());
            await RunTaskScheduler("/Run", "/TN", task);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            while (!File.Exists(result))
            {
                await Task.Delay(100, timeout.Token);
            }

            return File.ReadAllText(result);
        }
        finally
        {
            await RunTaskScheduler("/Delete", "/TN", task, "/F");
            Directory.Delete(directory, recursive: true);
        }
    }

    private static async Task RunTaskScheduler(params string[] arguments)
    {
        var start = new ProcessStartInfo("schtasks.exe") { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var argument in arguments)
        {
            start.ArgumentList.Add(argument);
        }

        using var process = Process.Start(start) ?? throw new InvalidOperationException("Task Scheduler did not start.");
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        await Task.WhenAll(output, error);
        Assert.Equal(0, process.ExitCode);
    }
}

public class AdministratorFactAttribute : FactAttribute
{
    public AdministratorFactAttribute()
    {
        using var identity = WindowsIdentity.GetCurrent();
        if (!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))
        {
            Skip = "Requires an elevated Windows test host to create a temporary SYSTEM task.";
        }
    }
}

public sealed class InteractiveAdministratorFactAttribute : AdministratorFactAttribute
{
    public InteractiveAdministratorFactAttribute()
    {
        if (Process.GetCurrentProcess().SessionId == 0 &&
            Environment.GetEnvironmentVariable("NAMEPLATE_REQUIRE_SESSION_PROOF") != "1")
        {
            Skip = "Requires an interactive Windows session; CI runs this against a session-zero task.";
        }
    }
}
