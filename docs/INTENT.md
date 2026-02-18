# YTPlex

## Purpose

Youtube has a lot of great content, but lately they're shipping features that are dentrimental to my mental health and I can't pay them to take them away.

Shorts are by far the worst thing to happen to Youtube, and me. They are so enticing that often I find myself lost in them when I opened Youtube to purposely look at a channel.

Then there's ads. Not the ones that go away with Youtube premium. I'm talking about the sponsors and the self promotion. Ugh! I don't care. I don't need a site on squarespace, I'm never going to use Brilliant, and I already have a VPN.

This app helps us take control of our video consumption by sourcing content from YouTube, removing distractions like sponsors or irrelevant segments, and letting us watch them in Plex. It’s built for most users who want a simple, well designed tool, without unnecessary complexity.

## Why Build This App?

I created this because existing tools didn’t fully match our needs. While some competitors had potential, either video processing didn’t work as I wanted, development stalled or they were just too complicated to setup. I want a tool that's' easy to use from the start but also flexible enough for advanced users.

## Design Principles

Keep the interface minimal and intuitive, so you can get started immediately.

Each component has a clear, simple API, allowing them to be combined in new ways as you need them. We wanted a structure like Legos or Excel—simple building blocks that open up possibilities once you understand them.

This is not an app for everybody. Instead it's an app for the majority of us that just want something that works reliably, it's easy to get started, feels smart to the user. It should not require a manual to learn how to operate, everything is clear and good defaults make features I don't need easy to ignore.

It's also not a passive app. It will recommend when to scan a channel, how long to wait for sponsorblock removal, maybe it even does this automatically.

Debuggable: every action should have a trace so by going through the logs I can easily see why a video is in that current form. And the logs have to be available in the app, no having to SSH into the machine.
