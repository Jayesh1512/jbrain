mod api;
mod ask;
mod brain;
mod cli;
mod error;
mod evidence;
mod mcp;
mod okf;
mod retrieval;
mod storage;

fn main() {
    let result = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(2)
        .build()
        .expect("create runtime")
        .block_on(cli::run());
    if let Err(error) = result {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
