import click
import asyncio
from .config import AppConfig, load_config
from .monitor import Monitor
from .pipeline import process_single_file


@click.group()
@click.pass_context
def cli(ctx):
    """Streamliner-AI: Herramienta de automatización de clips para Kick."""
    ctx.obj = load_config()


@cli.command()
@click.pass_obj
def monitor(config: AppConfig):
    """Inicia el modo de monitorización continua para los streamers configurados."""
    click.echo("🚀 Iniciando modo de monitorización...")
    m = Monitor(config)
    try:
        asyncio.run(m.start())
    except KeyboardInterrupt:
        click.echo("\n🛑 Deteniendo monitorización...")


@cli.command()
@click.option(
    "--file",
    "video_path",
    required=True,
    type=click.Path(exists=True),
    help="Ruta al archivo de video local.",
)
@click.option(
    "--streamer", required=True, help="Nombre del streamer para asociar el clip."
)
@click.option(
    "--dry-run", is_flag=True, help="Ejecuta todo el pipeline sin subir el video final."
)
@click.pass_obj
def process(config: AppConfig, video_path: str, streamer: str, dry_run: bool):
    """Procesa un archivo de video local para generar y (opcionalmente) subir un clip."""
    click.echo(f"⚙️  Procesando archivo local: {video_path}")
    if dry_run:
        click.echo("🌵 Modo Dry-Run activado. El clip no se subirá a TikTok.")

    asyncio.run(process_single_file(config, video_path, streamer, dry_run))


@cli.command()
def setup():
    """Genera archivos de configuración de ejemplo (.env.template, config.yaml.example)."""
    # Esta lógica crearía los archivos de ejemplo en el directorio actual.
    # Por simplicidad, el usuario los tiene directamente en el repo.
    click.echo(
        "✅ Archivos de ejemplo `.env.template` y `config.yaml.example` están disponibles en el repositorio."
    )
    click.echo("   Renómbrelos a `.env` y `config.yaml` y edite los valores.")


if __name__ == "__main__":
    cli()
