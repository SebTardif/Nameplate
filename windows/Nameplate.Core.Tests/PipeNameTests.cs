using Nameplate.Core;
using Xunit;

namespace Nameplate.Core.Tests;

public sealed class PipeNameTests
{
    [Fact]
    public void EachSessionHasItsOwnPipe()
    {
        Assert.Equal("nameplate-1", PipeNames.ForSession(1));
        Assert.Equal("nameplate-2", PipeNames.ForSession(2));
        Assert.NotEqual(PipeNames.ForSession(1), PipeNames.ForSession(2));
        Assert.NotEqual("nameplate", PipeNames.ForSession(1));
    }
}
