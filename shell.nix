{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
buildInputs = with pkgs; [
python3
just
ruff
mypy
];
}
