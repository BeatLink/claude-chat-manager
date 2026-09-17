{
    description = "claude-chat-manager - browse, summarize and delete local Claude Code conversations";

    inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    outputs =
        { self, nixpkgs }:
        let
            systems = [
                "x86_64-linux"
                "aarch64-linux"
            ];
            forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
        in
        {
            packages = forAllSystems (pkgs: {
                default = pkgs.python3Packages.buildPythonApplication {
                    pname = "claude-chat-manager";
                    version = "0.3.0";
                    src = ./.;
                    pyproject = true;
                    build-system = [ pkgs.python3Packages.setuptools ];
                    dependencies = with pkgs.python3Packages; [
                        textual
                        pygobject3
                    ];
                    nativeBuildInputs = [
                        pkgs.gobject-introspection
                        pkgs.wrapGAppsHook4
                    ];
                    buildInputs = [
                        pkgs.gtk4
                        pkgs.libadwaita
                    ];
                    # The claude CLI is called at runtime and is expected on the user's PATH.
                    dontWrapGApps = false;
                    doCheck = false;
                    postInstall = ''
                        install -Dm644 share/dev.beatlink.ClaudeChatManager.desktop \
                            $out/share/applications/dev.beatlink.ClaudeChatManager.desktop
                    '';
                    meta = {
                        description = "Browse, summarize and delete local Claude Code conversations";
                        mainProgram = "claude-chat-manager";
                    };
                };

                # The editor extension: a button on a conversation tab that closes it and calls the tool above.
                # home-manager's editor module reads these three to name the extension's directory.
                vscode-extension =
                    pkgs.runCommand "vscode-claude-chat-manager"
                        {
                            passthru = {
                                vscodeExtUniqueId = "beatlink.claude-chat-manager";
                                vscodeExtPublisher = "beatlink";
                                vscodeExtName = "claude-chat-manager";
                            };
                        }
                        ''
                            target=$out/share/vscode/extensions/beatlink.claude-chat-manager
                            mkdir -p $target
                            cp ${./vscode-extension}/package.json ${./vscode-extension}/extension.js $target/
                        '';
            });

            devShells = forAllSystems (pkgs: {
                default = pkgs.mkShell {
                    packages = [
                        (pkgs.python3.withPackages (ps: [
                            ps.textual
                            ps.pygobject3
                            ps.pytest
                        ]))
                        pkgs.gtk4
                        pkgs.libadwaita
                        pkgs.gobject-introspection
                    ];
                    shellHook = ''
                        export PYTHONPATH=$PWD:$PYTHONPATH
                    '';
                };
            });
        };
}
